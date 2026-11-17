"""QA layer tests: oracle, invariants, metamorphic relations, faults, reduction.

Run with::

    PYTHONPATH=src:. python3 -m unittest discover -s tests -v

These tests exercise the *quality assurance layer itself*: the oracle must
agree with the pipeline on boundary cases, the invariants must reject corrupted
results, metamorphic relations must hold, planted defects must be caught and
the reducer must produce minimal counterexamples.  The pipeline's own tests
live in ``test_core.py`` and ``test_pipeline.py``.
"""

import json
import unittest
from decimal import Decimal

from qa import faults, generators, invariants, metamorphic, oracle, reduce
from usage_contract_replay.engine import calculate
from usage_contract_replay.parser import parse
from usage_contract_replay.pipeline import run


class TestOracle(unittest.TestCase):
    def test_boundary_cases_match_pipeline(self):
        for payload in generators.boundary_payloads():
            with self.subTest(payload=payload):
                expected = oracle.compute(payload)
                result = run(payload)
                invariants.check_all(result, payload)
                self.assertEqual(result["total_units"], expected.total_units)
                self.assertEqual(result["billable_units"], expected.billable_units)
                self.assertEqual(result["free_units"], expected.free_units_used)
                self.assertEqual(result["amount_exact"], f"{expected.amount_cents / 100:.2f}")

    def test_empty_event_stream(self):
        payload = generators.make_payload([], free=10, unit_price="2.5")
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_units"], 0)
        self.assertEqual(result["billable_units"], 0)
        self.assertEqual(result["amount"], 0.0)
        self.assertIsNone(result["account"])
        self.assertEqual(result["events"], [])

    def test_zero_units_are_retained(self):
        payload = generators.make_payload([generators.make_event(units=0)], unit_price="2.5")
        result = run(payload)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(result["events"][0]["units"], 0)
        self.assertEqual(result["amount"], 0.0)

    def test_refund_above_gross_is_capped_not_negative(self):
        payload = generators.make_payload(
            [generators.make_event(units=1)], unit_price="2.5", refund="99", refund_reason="over"
        )
        result = run(payload)
        self.assertEqual(result["amount_exact"], "0.00")
        self.assertGreaterEqual(result["amount"], 0.0)
        self.assertTrue(result["refund_capped"])
        self.assertEqual(result["refund_reason"], "over")

    def test_half_up_rounding_boundary(self):
        # 0.005 * 1 == exactly half a cent; half-up gives 0.01, half-even 0.00.
        payload = generators.make_payload([generators.make_event(units=1)], unit_price="0.005")
        result = run(payload)
        self.assertEqual(result["amount_exact"], "0.01")
        # 1.005 * 1 also rounds up, not to 1.00 (the float trap).
        payload = generators.make_payload([generators.make_event(units=1)], unit_price="1.005")
        result = run(payload)
        self.assertEqual(result["amount_exact"], "1.01")

    def test_large_integers_do_not_overflow_or_truncate(self):
        payload = generators.make_payload([generators.make_event(units=2**53)], unit_price="1")
        result = run(payload)
        self.assertEqual(result["total_units"], 2**53)
        self.assertEqual(result["billable_units"], 2**53)

    def test_invalid_codes_match_oracle(self):
        for payload in generators.invalid_payloads():
            with self.subTest(payload=payload):
                expected = oracle.codes_for(payload)
                self.assertIsNotNone(expected)
                result = run(payload)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], expected)


class TestInvariantsRejectCorruption(unittest.TestCase):
    """The invariant checker must fail loud on a corrupted result."""

    def _valid(self):
        payload = generators.make_payload(
            [generators.make_event(units=12), generators.make_event(at="2026-01-16", units=8)],
            free=10,
            unit_price="2.5",
        )
        return payload, run(payload)

    def _assert_violation(self, result, payload, code):
        with self.assertRaises(invariants.InvariantViolation) as context:
            invariants.check_all(result, payload)
        self.assertEqual(context.exception.code, code)

    def test_detects_total_drift(self):
        payload, result = self._valid()
        result["total_units"] += 1
        self._assert_violation(result, payload, "total_units_conservation")

    def test_detects_billable_drift(self):
        payload, result = self._valid()
        result["billable_units"] += 1
        self._assert_violation(result, payload, "billable_conservation")

    def test_detects_detail_order(self):
        payload, result = self._valid()
        result["events"] = list(reversed(result["events"]))
        self._assert_violation(result, payload, "event_order")

    def test_detects_negative_amount(self):
        payload, result = self._valid()
        result["amount_exact"] = "-1.00"
        result["amount"] = -1.0
        result["refund_applied_exact"] = "26.00"
        # gross 25 - applied 26 == -1 reconciles, so the first violation
        # surfaced is the non-negativity guard.
        self._assert_violation(result, payload, "amount_negative")

    def test_detects_uncapped_refund(self):
        payload, result = self._valid()
        # Keep amount = gross - applied so reconciliation passes, but set the
        # applied refund above the requested amount and above gross.
        gross = Decimal(result["gross_amount_exact"])
        result["refund_requested_exact"] = "1.00"
        result["refund_applied_exact"] = format(gross, "f")
        result["amount_exact"] = "0.00"
        result["amount"] = 0.0
        self._assert_violation(result, payload, "refund_apply")

    def test_detects_missing_reason(self):
        payload = generators.make_payload(
            [generators.make_event(units=2)], unit_price="1", refund="1", refund_reason="x"
        )
        result = run(payload)
        result["refund_reason"] = None
        self._assert_violation(result, payload, "refund_reason_missing")

    def test_detects_line_amount_drift(self):
        payload, result = self._valid()
        result["events"][0]["line_amount"] = "999.99"
        self._assert_violation(result, payload, "line_amount")


class TestMetamorphic(unittest.TestCase):
    def test_relations_hold_on_generated_cases(self):
        for payload in generators.valid_payloads(seed=7, count=25):
            with self.subTest(payload=payload):
                metamorphic.apply(run, payload, seed=11)
                metamorphic.split_totals_preserved(run, payload, seed=11)

    def test_replay_is_idempotent(self):
        payload = generators.make_payload([generators.make_event(units=5)], unit_price="1")
        base = run(payload)
        replayed = run(metamorphic.duplicate_events(payload))
        triple = run(metamorphic.duplicate_events(metamorphic.duplicate_events(payload)))
        for other in (replayed, triple):
            self.assertEqual(other["amount_exact"], base["amount_exact"])
            self.assertEqual(other["total_units"], base["total_units"])

    def test_order_independence(self):
        payload = generators.make_payload(
            [
                generators.make_event(at="2026-01-03", units=4),
                generators.make_event(at="2026-01-01", units=6),
                generators.make_event(at="2026-01-02", units=2),
            ],
            free=5,
            unit_price="2.5",
        )
        base = run(payload)
        for seed in range(20):
            shuffled = metamorphic.reorder_events(payload, seed)
            other = run(shuffled)
            self.assertEqual(other["amount_exact"], base["amount_exact"])
            self.assertEqual(
                [item["free_units"] for item in other["events"]],
                [item["free_units"] for item in base["events"]],
            )


class TestFaultInjection(unittest.TestCase):
    def test_all_planted_defects_are_caught(self):
        survivors = faults.surviving_mutants(seed=20261117, count=40)
        self.assertEqual(survivors, [], f"undetected planted defects: {survivors}")

    def test_a_known_bug_is_caught(self):
        payload = generators.make_payload([generators.make_event(units=3)], free=1, unit_price="1")
        mutant = [m for m in faults.MUTANTS if m[0] == "free_off_by_one"][0]
        self.assertTrue(faults.caught_by(payload, mutant))


class TestReduction(unittest.TestCase):
    def test_reducer_reaches_strict_local_minimum(self):
        payload = generators.make_payload(
            [generators.make_event(at=f"2026-01-{i:02d}", units=u) for i, u in enumerate([4, 3, 5, 2, 6])],
            free=3,
            unit_price="2.5",
            refund="1",
            refund_reason="x",
        )

        def fails(case):
            return sum(event["units"] for event in case["events"]) >= 10

        minimal, steps = reduce.minimize(payload, fails)
        self.assertTrue(fails(minimal))
        self.assertLess(reduce.complexity(minimal), reduce.complexity(payload))
        for candidate in reduce._candidates(minimal):
            self.assertFalse(
                fails(candidate) and reduce.complexity(candidate) < reduce.complexity(minimal),
                f"not minimal: {candidate}",
            )

    def test_reducer_leaves_passing_case_alone(self):
        payload = generators.make_payload([generators.make_event(units=1)], unit_price="1")
        minimal, steps = reduce.minimize(payload, lambda case: False)
        self.assertEqual(minimal, payload)
        self.assertEqual(steps, 0)

    def test_reduction_is_deterministic(self):
        payload = generators.boundary_payloads()[7]
        first = reduce.minimize(payload, lambda case: False)[0]
        second = reduce.minimize(payload, lambda case: False)[0]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

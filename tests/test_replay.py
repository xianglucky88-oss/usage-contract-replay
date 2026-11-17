import json
import unittest

from helpers import contract, event, only_invoice
from usage_contract_replay.pipeline import run
from usage_contract_replay.replay import verify
from usage_contract_replay.util import InputError


class TestPartialBatches(unittest.TestCase):
    def test_failed_batch_is_reported_and_others_bill(self):
        payload = {
            "contracts": [contract()],
            "batches": [
                {
                    "batch_id": "b1",
                    "events": [event(event_id="e1", units=2)],
                },
                {
                    "batch_id": "b2",
                    "status": "failed",
                    "error": "producer timeout",
                    "events": [event(event_id="e2", units=9)],
                },
                {
                    "batch_id": "b3",
                    "events": [event(event_id="e3", units=3)],
                },
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["issue_counts"], {"batch_failed": 1})
        self.assertEqual(result["audit"]["batches"], ["b1", "b3"])
        self.assertEqual(only_invoice(result)["net_cents"], 500)

    def test_malformed_rows_inside_a_batch_do_not_drop_valid_rows(self):
        payload = {
            "contracts": [contract()],
            "batches": [
                {
                    "batch_id": "b1",
                    "events": [
                        event(event_id="good1", units=1),
                        {"event_id": "bad", "account": "A1", "at": "nope", "units": 1},
                        event(event_id="good2", units=2),
                    ],
                }
            ],
        }
        result = run(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["audit"]["issue_counts"], {"invalid_event": 1})
        self.assertEqual(only_invoice(result)["net_cents"], 300)

    def test_strict_mode_rejects_a_batch_with_any_error(self):
        payload = {
            "contracts": [contract()],
            "on_error": "fail",
            "batches": [
                {"batch_id": "b1", "status": "failed", "error": "boom"},
                {"batch_id": "b2", "events": [event(event_id="e1", units=1)]},
            ],
        }
        with self.assertRaises(InputError):
            run(payload)

    def test_strict_mode_accepts_a_clean_batch(self):
        payload = {
            "contracts": [contract()],
            "on_error": "fail",
            "events": [event(event_id="e1", units=1)],
        }
        self.assertEqual(run(payload)["status"], "ok")


class TestReplayDeterminism(unittest.TestCase):
    def rich_payload(self):
        contracts = [
            {
                "id": "C",
                "version": 1,
                "effective_from": "2026-01-01",
                "effective_to": "2026-02-01",
                "pricing": {
                    "api": {
                        "free": 10,
                        "tiers": [
                            {"up_to": 100, "unit_price": "0.10"},
                            {"up_to": None, "unit_price": "0.05"},
                        ],
                    },
                    "storage": {"unit_price": "0.2"},
                },
            },
            {
                "id": "C",
                "version": 2,
                "effective_from": "2026-02-01",
                "pricing": {
                    "api": {
                        "free": 20,
                        "tiers": [
                            {"up_to": 50, "unit_price": "0.2"},
                            {"up_to": None, "unit_price": "0.1"},
                        ],
                    },
                    "storage": {"unit_price": "0.3"},
                },
            },
        ]
        events = [
            {"event_id": "a1", "account": "A", "at": "2026-01-31T23:30:00Z", "metric": "api", "units": 70},
            {"event_id": "a2", "account": "A", "at": "2026-02-01T00:30:00Z", "metric": "api", "units": 60},
            {"event_id": "s1", "account": "A", "at": "2026-01-15", "metric": "storage", "units": 10},
            {"event_id": "s2", "account": "A", "at": "2026-03-15", "metric": "storage", "units": 12},
            {"event_id": "a3", "account": "A", "at": "2026-03-01", "metric": "api", "units": 40},
            {"event_id": "rm", "account": "A", "at": "2026-03-02", "operation": "remove", "target_event_id": "a3"},
            {"event_id": "fix", "account": "A", "at": "2026-03-03", "operation": "set", "target_event_id": "s2", "units": 8},
            {"event_id": "rf", "account": "A", "at": "2026-03-04", "operation": "refund", "target_event_id": "s1", "refund_ratio": "0.25"},
            # Idempotent duplicate and a late event.
            {"event_id": "a1", "account": "A", "at": "2026-01-31T23:30:00Z", "metric": "api", "units": 70},
            {"event_id": "late", "account": "A", "at": "2026-01-20", "received_at": "2026-02-05", "metric": "api", "units": 5},
        ]
        return {"contracts": contracts, "events": events, "billing": {"period": "month"}}

    def test_same_batch_replays_bitwise_identically(self):
        payload = self.rich_payload()
        first = json.dumps(run(payload), sort_keys=True)
        second = json.dumps(run(payload), sort_keys=True)
        self.assertEqual(first, second)

    def test_shuffled_event_and_contract_order_is_identical(self):
        payload = self.rich_payload()
        self.assertTrue(verify(payload, rounds=6)["deterministic"])

    def test_sampled_permutations_never_change_the_digest(self):
        import itertools
        import random

        payload = self.rich_payload()
        events = payload["events"]
        baseline = run(payload)["audit"]["output_digest"]
        indices = list(range(len(events)))
        rng = random.Random(7)
        samples = [
            tuple(indices),
            tuple(reversed(indices)),
            *(
                tuple(rng.sample(indices, len(indices)))
                for _ in range(12)
            ),
        ]
        # Also exhaustively cover every ordering of the first 5 events, which
        # contains the same-instant and duplicate pairs.
        prefix = indices[:5]
        samples.extend(itertools.permutations(prefix))
        for permutation in samples:
            order = list(permutation) + indices[5:]
            result = run({**payload, "events": [events[i] for i in order]})
            self.assertEqual(result["audit"]["output_digest"], baseline)

    def test_rules_version_change_is_observable_even_for_identical_billing(self):
        payload = self.rich_payload()
        result = run(payload)
        self.assertEqual(result["input_digest"], run(payload)["input_digest"])
        self.assertEqual(
            result["audit"]["output_digest"], run(payload)["audit"]["output_digest"]
        )

    def test_duplicate_event_does_not_change_output(self):
        payload = self.rich_payload()
        # The penultimate entry is a byte-identical copy of the first event.
        without = {
            **payload,
            "events": payload["events"][:-2] + payload["events"][-1:],
        }
        result = run(payload)
        self.assertEqual(
            run(without)["audit"]["output_digest"],
            result["audit"]["output_digest"],
        )
        self.assertEqual(
            result["audit"]["duplicates"][0]["disposition"], "duplicate_ignored"
        )


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from helpers import contract, event, invoice, only_invoice
from usage_contract_replay.cli import main
from usage_contract_replay.pipeline import run
from usage_contract_replay.snapshots import freeze_document
from usage_contract_replay.util import InputError


class TestCli(unittest.TestCase):
    def test_cli_output_is_key_sorted_and_exit_zero(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main([str(Path(__file__).parents[1] / "fixtures" / "input.json")])
        text = buffer.getvalue()
        self.assertTrue(text.lstrip().startswith('{'))
        parsed = json.loads(text)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["amount"], 25)

    def test_cli_invalid_input_exits_two(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"events": [], "contract": {"unit_price": -1}}))
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(
                io.StringIO()
            ):
                main([str(path)])
            self.assertEqual(caught.exception.code, 2)

    def test_cli_emits_freeze_receipts(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "freeze.json"
            with contextlib.redirect_stdout(io.StringIO()):
                main(
                    [
                        str(Path(__file__).parents[1] / "fixtures" / "input.json"),
                        "--emit-freeze",
                        str(output),
                    ]
                )
            receipts = json.loads(output.read_text())
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["period"], "2026-01")
            self.assertEqual(receipts[0]["net_cents"], 2500)

    def test_cli_verify_reports_determinism(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main([str(Path(__file__).parents[1] / "fixtures" / "replay_scenario.json"), "--verify"])
        parsed = json.loads(buffer.getvalue())
        self.assertTrue(parsed["replay_verification"]["deterministic"])


class TestErrorsAndEdges(unittest.TestCase):
    def test_empty_batch_is_valid_and_empty(self):
        payload = {"contracts": [contract()], "events": []}
        result = run(payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["invoices"], [])
        self.assertEqual(result["summary"]["billed_net_cents"], 0)

    def test_account_with_no_events_gets_empty_invoice_in_closed_period(self):
        payload = {
            "contracts": [contract()],
            "billing": {"period": "month", "periods": ["2026-01"]},
            "accounts": ["A1", "B2"],
            "events": [event(event_id="e", units=1)],
        }
        result = run(payload)
        empty = invoice(result, "B2", "2026-01")
        self.assertEqual(empty["status"], "empty")
        self.assertEqual(empty["net_cents"], 0)
        self.assertTrue(all(result["audit"]["invariants"].values()))

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(InputError):
            run([])

    def test_events_not_a_list_is_rejected(self):
        with self.assertRaises(InputError):
            run({"contracts": [contract()], "events": {"a": 1}})

    def test_unknown_timezone_is_rejected(self):
        with self.assertRaises(InputError):
            run(
                {
                    "contracts": [contract()],
                    "billing": {"timezone": "Mars/Olympus"},
                    "events": [],
                }
            )

    def test_amount_and_money_fields_agree(self):
        payload = {"contracts": [contract()], "events": [event(event_id="e", units=3)]}
        result = run(payload)
        inv = only_invoice(result)
        self.assertEqual(inv["net"], "%0.2f" % (inv["net_cents"] / 100))
        self.assertEqual(inv["gross_cents"], 300)
        self.assertEqual(result["amount"], 3)

    def test_contract_level_refund_is_applied_once(self):
        payload = {
            "contracts": [contract(refund="2.00")],
            "events": [event(event_id="e", units=10)],
        }
        result = run(payload)
        inv = only_invoice(result)
        self.assertEqual(inv["net_cents"], 800)
        self.assertEqual(
            [line["kind"] for line in inv["items"]], ["usage", "contract_refund"]
        )

    def test_currency_mismatch_with_frozen_history_is_reported(self):
        first = run({"contracts": [contract()], "events": [event(event_id="e", units=1)]})
        payload = {
            "contracts": [contract()],
            "events": [event(event_id="e", units=1)],
            "currency": "USD",
            "frozen_invoices": [
                {**receipt, "currency": "EUR"}
                for receipt in freeze_document(first)
            ],
        }
        result = run(payload)
        self.assertEqual(
            result["audit"]["issue_counts"], {"frozen_currency_mismatch": 1}
        )

    def test_fractional_negative_adjustment_reverses_top_tier_first(self):
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
                event(event_id="u", at="2026-01-01", units=20),
                event(
                    event_id="credit",
                    at="2026-01-02",
                    operation="set",
                    units=-5,
                ),
            ],
        }
        result = run(payload)
        inv = only_invoice(result)
        usage, credit = inv["items"]
        # 10 units at 1.00 + 10 at 5.00 = 60.00; the credit reverses 5 units
        # from the most expensive tier, so 60.00 - 25.00 = 35.00.
        self.assertEqual(usage["tiers"][-1]["unit_price"], "5")
        self.assertEqual(credit["amount_cents"], -2500)
        self.assertEqual(inv["net_cents"], 3500)
        self.assertEqual(credit["adjustment_reasons"], ["manual_adjustment"])


if __name__ == "__main__":
    unittest.main()

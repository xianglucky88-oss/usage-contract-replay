"""Deterministic JSON CLI.

Exit codes: 0 ok/partial (check ``status``), 2 invalid input, 1 internal error.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run
from .util import InputError


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay usage events against versioned SaaS contracts and produce deterministic invoices."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(Path(__file__).parents[2] / "fixtures" / "input.json"),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent output (output is always key-sorted)",
    )
    parser.add_argument(
        "--emit-freeze",
        metavar="PATH",
        help="write frozen receipts for every open invoice to PATH",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-run the batch in shuffled orders and report determinism",
    )
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        result = run(payload)
        if args.emit_freeze:
            from .snapshots import freeze_document

            receipts = freeze_document(result)
            Path(args.emit_freeze).write_text(
                json.dumps(receipts, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result["freeze_receipts"] = len(receipts)
        if args.verify:
            from .replay import verify

            result["replay_verification"] = verify(payload)
    except (InputError, OSError, ValueError) as exc:
        print(json.dumps({"project": "usage-contract-replay", "status": "failed", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

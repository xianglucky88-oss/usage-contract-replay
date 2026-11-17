"""Command-line entry point for the QA layer.

Examples::

    PYTHONPATH=src:. python3 -m qa.cli property --seed 7 --count 200
    PYTHONPATH=src:. python3 -m qa.cli metamorphic --seed 7 --count 200
    PYTHONPATH=src:. python3 -m qa.cli faults
    PYTHONPATH=src:. python3 -m qa.cli mutation
    PYTHONPATH=src:. python3 -m qa.cli all

``--json`` emits a single machine-readable report.  A non-zero exit code means
the corresponding layer found a problem; the printed payload (or its
``failures`` list) contains minimal counterexamples where applicable.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

from . import checks, mutation


def _run(args) -> Dict[str, Any]:
    report: Dict[str, Any] = {"status": "ok", "command": args.command}
    failures: List[Dict[str, Any]] = []
    if args.command in ("property", "all"):
        failures += checks.check_properties(args.seed, args.count)
    if args.command in ("metamorphic", "all"):
        failures += checks.check_metamorphic(args.seed, args.count)
    if args.command in ("faults", "all"):
        failures += checks.check_faults(args.seed, args.count)
    if args.command in ("report", "all"):
        failures += checks.check_report_layer(args.seed)
    if args.command in ("mutation", "all"):
        killed, survived, errors = mutation.run_mutation_testing()
        report["mutation"] = {
            "killed": killed,
            "survived": survived,
            "errors": errors,
            "score": f"{len(killed)}/{len(killed) + len(survived)}",
        }
        for name in survived:
            failures.append({"kind": "source_mutation", "mutant": name, "error": "mutant survived"})
        for error in errors:
            failures.append({"kind": "source_mutation", "error": error})
    report["failures"] = failures
    if failures:
        report["status"] = "failed"
    return report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="usage-contract-replay QA oracle")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["property", "metamorphic", "faults", "report", "mutation", "all"],
    )
    parser.add_argument("--seed", type=int, default=20261117, help="deterministic generator seed")
    parser.add_argument("--count", type=int, default=60, help="generated cases per layer")
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    args = parser.parse_args(argv)

    report = _run(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"status: {report['status']}")
        for failure in report["failures"]:
            print(f"- [{failure.get('kind')}] {failure.get('error')}")
            if "minimal" in failure:
                print("  minimal:", json.dumps(failure["minimal"], sort_keys=True))
        if "mutation" in report:
            print("mutation score:", report["mutation"]["score"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Mutation testing of the production source.

Two levels are provided:

* :mod:`qa.faults` injects behavioural mutants in-process (fast, and directly
  names the bug classes in the brief).
* this module mutates the *source text* of the production modules, writes the
  mutated package to a temporary tree, and runs the QA suite against it in a
  subprocess.  A mutation is killed if the suite fails; survivors are reported.

Correctness guards for the runner itself:

* the suite is invoked with ``-I`` (isolated) and the tree is placed on
  ``sys.path`` *inside* the child, because ``-I`` intentionally ignores
  ``PYTHONPATH``;
* a **clean** copy of the tree must pass the suite before any mutant is scored.
  Without this check an import error would look like "every mutant killed".
* each mutated anchor must be present and unique in the baseline source.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

SRC = Path(__file__).resolve().parents[1] / "src"
QA = Path(__file__).resolve().parent

_CHILD = (
    "import sys; "
    "roots = sys.argv[1:3]; sys.argv = [sys.argv[0]] + sys.argv[3:]; "
    "sys.path[:0] = roots; "
    "from qa.checks import main; raise SystemExit(main())"
)


@dataclass(frozen=True)
class Mutation:
    name: str
    category: str
    file: str
    old: str
    new: str
    description: str


MUTATIONS: List[Mutation] = [
    Mutation(
        "free_minus_one",
        "free_truncation",
        "engine.py",
        "remaining_free = free_units",
        "remaining_free = max(0, free_units - 1)",
        "shift the free boundary by one unit",
    ),
    Mutation(
        "free_ignored",
        "free_truncation",
        "engine.py",
        "used_free = min(event.units, remaining_free)",
        "used_free = 0",
        "ignore the free quota entirely",
    ),
    Mutation(
        "round_half_down",
        "float_rounding",
        "engine.py",
        "return value.quantize(CENT, rounding=ROUND_HALF_UP)",
        "return value.quantize(CENT, rounding=__import__('decimal').ROUND_HALF_DOWN)",
        "round half down instead of half up",
    ),
    Mutation(
        "truncate_cents",
        "float_rounding",
        "engine.py",
        "return value.quantize(CENT, rounding=ROUND_HALF_UP)",
        "return value.quantize(CENT, rounding=__import__('decimal').ROUND_DOWN)",
        "truncate cents instead of rounding",
    ),
    Mutation(
        "refund_uncapped_source",
        "refund_boundary",
        "engine.py",
        "applied = min(requested, gross)",
        "applied = requested",
        "do not cap the refund at gross",
    ),
    Mutation(
        "sort_removed",
        "account_grouping",
        "parser.py",
        "unique.sort(key=lambda event: event.sort_key)",
        "pass",
        "remove canonical ordering",
    ),
    Mutation(
        "dedupe_removed",
        "replay_idempotence",
        "parser.py",
        "key = event.identity",
        'key = ("unique", id(event))',
        "disable replay de-duplication",
    ),
    Mutation(
        "account_dropped",
        "account_grouping",
        "engine.py",
        "account = parsed.events[0].account if parsed.events else None",
        "account = None",
        "drop account grouping from the invoice",
    ),
    Mutation(
        "report_sort_removed",
        "report_wrapping",
        "report.py",
        'payload["events"] = _stable_events(events)',
        'payload["events"] = list(events)',
        "drop the report's stable re-sort",
    ),
    Mutation(
        "report_version_wrong",
        "report_wrapping",
        "report.py",
        "REPORT_VERSION = 1",
        "REPORT_VERSION = 2",
        "bump the envelope version without a schema change",
    ),
    Mutation(
        "units_int_coercion",
        "integer_conversion",
        "parser.py",
        '    if isinstance(value, bool) or not isinstance(value, int):\n'
        '        raise InputError(\n'
        '            "units_not_int",\n'
        '            f"units must be an integer, got {type(value).__name__}",\n'
        '            path,\n'
        '        )\n',
        '    if not isinstance(value, int):\n        value = int(value)\n',
        "coerce non-integer units with int() instead of rejecting them",
    ),
]


def _apply_mutation(tree: Path, mutation: Mutation) -> None:
    path = tree / "usage_contract_replay" / mutation.file
    text = path.read_text()
    if mutation.old not in text:
        raise RuntimeError(f"mutation anchor not found in {mutation.file}: {mutation.old!r}")
    if text.count(mutation.old) != 1:
        raise RuntimeError(f"mutation anchor is not unique in {mutation.file}: {mutation.old!r}")
    path.write_text(text.replace(mutation.old, mutation.new, 1))


def _prepare(tmp: str) -> Tuple[Path, Path]:
    tree = Path(tmp)
    shutil.copytree(SRC, tree / "src")
    shutil.copytree(QA, tree / "qa")
    return tree, tree / "src"


def _run_suite(tree: Path) -> Tuple[int, str]:
    package_root = tree / "src"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD, str(package_root), str(tree), "--suite"],
        cwd=str(tree),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def run_mutation_testing(baseline_required: bool = True) -> Tuple[List[str], List[str], List[str]]:
    """Return ``(killed, survived, errors)`` mutation names.

    If ``baseline_required`` and a clean tree fails the suite, every mutation
    is reported as an error rather than a kill — a broken harness must never
    be mistaken for a strong one.
    """
    killed: List[str] = []
    survived: List[str] = []
    errors: List[str] = []

    if baseline_required:
        with tempfile.TemporaryDirectory() as tmp:
            tree, _ = _prepare(tmp)
            code, output = _run_suite(tree)
            if code != 0:
                return [], [], [f"baseline sanity check failed (rc={code}): {output[-500:]}"]

    for mutation in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            tree, package_root = _prepare(tmp)
            try:
                _apply_mutation(package_root, mutation)
            except RuntimeError as error:
                errors.append(f"{mutation.name}: {error}")
                continue
            code, _output = _run_suite(tree)
            if code != 0:
                killed.append(mutation.name)
            else:
                survived.append(mutation.name)
    return killed, survived, errors

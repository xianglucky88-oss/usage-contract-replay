"""Replay verification helpers.

The engine is a pure function of the raw batch.  These helpers make that
claim testable in production: re-run the same batch with the input order
deterministically shuffled and compare the invoice digests.  A mismatch means
something consulted producer order, dict order, or wall-clock time.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from .pipeline import run
from .util import canonical_json


def _shuffled_payload(payload: Mapping[str, Any], seed: int) -> dict:
    rng = random.Random(seed)

    def shuffle(sequence):
        if not isinstance(sequence, list):
            return sequence
        copied = list(sequence)
        rng.shuffle(copied)
        return copied

    shuffled = dict(payload)
    for key in ("events", "contracts", "accounts", "environments"):
        if key in shuffled:
            shuffled[key] = shuffle(shuffled[key])
    if isinstance(shuffled.get("frozen_invoices"), list):
        shuffled["frozen_invoices"] = shuffle(shuffled["frozen_invoices"])
    return shuffled


def verify(payload: Mapping[str, Any], rounds: int = 3) -> dict:
    """Compare the canonical replay against shuffled re-runs."""

    baseline = run(payload)
    expected = canonical_json(baseline)
    mismatches = []
    for seed in range(rounds):
        candidate = run(_shuffled_payload(payload, seed))
        if canonical_json(candidate) != expected:
            mismatches.append(
                {
                    "seed": seed,
                    "baseline_digest": baseline["audit"]["output_digest"],
                    "candidate_digest": candidate["audit"]["output_digest"],
                }
            )
    return {
        "deterministic": not mismatches,
        "runs": rounds + 1,
        "output_digest": baseline["audit"]["output_digest"],
        "mismatches": mismatches,
    }

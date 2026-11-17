"""Independent quality-assurance layer for usage-contract-replay.

Nothing in this package is imported by the production modules.  The QA layer
is a *reference implementation* (``oracle``) plus the harnesses that compare
the production pipeline against it: property generators, metamorphic
relations, fault injection, minimal-counterexample reduction and mutation
testing.

Independence rules that keep this layer a genuine oracle:

* it never imports ``usage_contract_replay``;
* money is computed with :class:`fractions.Fraction` and integer cents, while
  the engine uses :class:`decimal.Decimal` — so a shared rounding bug cannot
  hide in both;
* it recomputes validation, canonical ordering, free-quota allocation and
  refund capping from the documented semantics.
"""

from __future__ import annotations

__all__ = ["oracle", "invariants", "generators", "metamorphic", "faults", "reduce", "mutation", "checks"]

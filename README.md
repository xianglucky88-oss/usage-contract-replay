# Usage Contract Replay

Replay usage events against versioned SaaS contracts and produce deterministic,
traceable invoices.

## Modules

- `src/usage_contract_replay/models.py`: immutable data contracts (no logic).
- `src/usage_contract_replay/parser.py`: input validation, canonical ordering,
  idempotent de-duplication, contract normalisation.
- `src/usage_contract_replay/engine.py`: domain calculation only.
- `src/usage_contract_replay/report.py`: stable, deterministic output envelope.
- `src/usage_contract_replay/pipeline.py`: orchestration boundary; converts
  validation failures into stable error envelopes.
- `src/usage_contract_replay/cli.py`: deterministic JSON CLI.

## Billing semantics

These are the rules the engine implements and the QA oracle independently
re-derives. They are boundaries, not new pricing rules:

- `units` must be a non-negative integer. Booleans, floats and numeric strings
  are **rejected**, never truncated with `int()`.
- `free` is a non-negative integer; the free quota is consumed in canonical
  event order and no event's allocation goes negative.
- `unit_price` and `refund` are exact decimal money; the aggregate is rounded
  **once**, half-up, to cents. Sub-cent unit prices are preserved.
- `refund` is capped at the gross amount receivable, so `amount >= 0`; the cap
  is reported (`refund_capped`) rather than hidden. A positive refund requires
  a non-empty `refund_reason`.
- Events are de-duplicated by idempotency key: `event_id` when present,
  otherwise `(account, at, units)`. Replaying the same stream is a no-op.
  The same `event_id` with a conflicting payload is rejected.
- All events in one replay must belong to one account; the account is
  normalised (surrounding whitespace stripped).
- `total_units = sum(units)`, `billable_units = sum(billable)`, and
  `free_units = total_units - billable_units` are conserved against the detail.
- The report sorts its event detail by `(account, at, event_id, units)` and is
  byte-stable for a given payload.

The contract used for the invoice is echoed under `contract` (plus
`contract_version`), so every input field is traceable from the output.

## Run

```bash
PYTHONPATH=src python3 -m usage_contract_replay.cli
PYTHONPATH=src:. python3 -m unittest discover -s tests -v
```

## QA layer (`qa/`)

The QA layer is independent of production code: it never imports
`usage_contract_replay`, and its oracle uses `fractions.Fraction` and integer
cents while the engine uses `decimal.Decimal`.

| Module | Purpose |
| --- | --- |
| `qa/oracle.py` | Reference implementation of the semantics above. |
| `qa/invariants.py` | Conservation, amount reconciliation, traceability and ordering checks; also cross-checks against the oracle. |
| `qa/generators.py` | Seedable generators for valid, boundary and invalid payloads. |
| `qa/metamorphic.py` | Order independence, replay idempotence, split/total preservation, report stability. |
| `qa/faults.py` | In-process planted defects (integer truncation, free truncation, float rounding, account grouping, replay double-count, report packaging). |
| `qa/reduce.py` | Greedy minimal-counterexample reduction. |
| `qa/mutation.py` | Source-text mutation testing with a clean-baseline guard. |
| `qa/checks.py` / `qa/cli.py` | Runnable suites. |

```bash
PYTHONPATH=src:. python3 -m qa.cli all            # every layer, including source mutation
PYTHONPATH=src:. python3 -m qa.cli property --seed 7 --count 200
PYTHONPATH=src:. python3 -m qa.cli mutation --json
```

Failures are printed with a reduced minimal counterexample and a stable
invariant/error code; a non-zero exit code signals failure.

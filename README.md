# Usage Contract Replay

Replay usage events against versioned SaaS contracts and produce deterministic,
auditable invoices. Given the same raw batch and the same rules version, the
output is byte-for-byte identical regardless of producer order, arrival order,
retries or duplicate deliveries.

```bash
PYTHONPATH=src python3 -m usage_contract_replay.cli fixtures/replay_scenario.json --pretty
PYTHONPATH=src python3 -m usage_contract_replay.cli fixtures/replay_scenario.json --verify
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

No network, credentials or clock access. Python 3.9+.

## Output at a glance

```
invoices[]          open (recomputable) invoices, one per account × period
frozen_invoices[]    closed invoices: the promised net plus the recomputation
revisions[]          frozen-period deltas that need an operator decision
audit.events[]       per-event disposition: billed / retracted / control / ...
audit.contract_lock  every contract version used, with its fingerprint
audit.rounding[]     lines whose exact total differs from the rounded total
audit.invariants     structural self-checks; status flips to partial on failure
summary              counts, net totals and the input/output digests
```

Every line item answers *why this amount*:

| field | meaning |
| --- | --- |
| `source_event_ids` | the usage event plus any correction events that changed it |
| `contract_version` | `account\|*:contract:vN`, pinned per line |
| `contract_fingerprint` | content hash of the terms that priced the line |
| `tiers[]` | per-tier units, unit price and amount after free allowance |
| `free_units` / `billable_units` | the allowance consumed and what remains |
| `adjustment_reasons` | `free_allowance`, `late_event`, `corrected`, `refund_capped_at_remaining`, ... |
| `target_event_id` / `target_period` | for refunds: what was reversed, in which period |

## Semantics (the contract of the engine)

### Identity, duplicates and conflicts

- Identity is `(account, event_id)`. Events without an id get a deterministic
  content-addressed id, so replaying an id-less feed is also idempotent.
- Identical identity + identical payload → **duplicate ignored**, counted in
  `audit.duplicates`.
- Identical identity + different payload → **conflict**. Neither copy is
  billed and the identity is excluded, because picking one would make the
  invoice depend on input order. The producer must send an explicit
  `remove` plus a new event.
- A row marked `retracted`/`void` is treated as retracted even if the
  corresponding tombstone has not been replayed.

### Ordering

Lines are allocated in `(event_time, event_id, account, metric, fingerprint)`
order. Same-instant events are therefore ordered by `event_id` — documented,
stable, and independent of arrival. Allocation (free allowance and tier
segments) follows that order, so "which event consumed the cheap tier" is a
property of the batch, not of the feed.

### Retraction, restore, correction

- `remove`/`restore` are tombstone events with their own `event_id` and a
  `target_event_id`. The greatest `(event_time, event_id)` wins; a tie is
  broken by `event_id`.
- A tombstone may arrive before its target and still wins — replay is over
  the final set, not over time.
- `set` with a `target_event_id` replaces the target's units; the original
  event id stays in `source_event_ids` and the line is marked `corrected`.
- `set` without a target is a signed manual adjustment line. Negative
  adjustments do not restore free allowance; they are priced against what the
  period already consumed (reverse tier consumption, top tier first).

### Periods and lateness

- Attribution uses **event time**, converted to the billing timezone, into a
  half-open window `[start, end)`. `received_at` never moves an event between
  periods; it only marks it `late_event` when it arrived at or after the
  period end.
- `billing.periods`, when given, is the authoritative closed set. Events
  outside it are reported as `period_out_of_window` instead of silently
  creating an invoice for a period nobody asked for.
- Configured periods produce an invoice for every known account, including an
  empty `status: "empty"` one, so "no activity" is distinguishable from
  "never evaluated".

### Contracts and version locking

- Versions are selected by `effective_from <= at < effective_to`; an
  account-specific contract beats a global one; the exact boundary instant
  belongs to the later version.
- Overlapping intervals are rejected (two answers for one instant). Gaps are
  not fatal: only the events inside the gap report `missing_contract`.
- Every run emits `audit.contract_lock` (sorted, fingerprinted) and a
  `contract_lock_digest`. Pass `expected_contract_lock_digest` to pin it: a
  mismatch aborts the run. This is how "the rules and terms are fixed" is
  enforced rather than assumed.
- `rules_version` is pinned by the build. Stamping an unknown version is an
  error, because the output digest claims that exact version produced it.

### Tiers, free allowance and rounding

- Free allowance is consumed first, in stable event order, then tier segments
  are allocated cumulatively across the pool. Scope is configurable:
  `contract_version` (default), `contract_family`, or `account`.
- If a pool would mix contract versions whose allowance or tier table differ,
  it is split per version and reported (`mixed_free_allowance` /
  `mixed_tier_table`) instead of applying one version's terms to another's
  usage.
- Units and prices are `Decimal`; multiplication and accumulation are exact.
  Each output line is rounded once (half away from zero) to integer cents.
  `audit.rounding` records any difference between the exact sum and the
  rounded line total.

### Refunds

- A refund is a monetary event: it is attributed to the **refund event's**
  period and references `target_event_id` / `target_period`. That is what lets
  a frozen January invoice stay frozen while a February credit points at it.
- The amount is capped at the target line's billed amount and at its
  unrefunded remainder; an over-refund becomes `refund_capped_at_remaining`
  plus a zero-amount `refund_zero` line, so the request is still visible.
- A ratio refund is applied to the target's billed cents and rounded once.
- Refunds against retracted usage are rejected (nothing to reverse).
- Target-less refunds are capped against the period's gross minus credits
  already issued there.
- `audit.invariants.refunds_within_billed_usage` proves total credits never
  exceed total billed usage, including across frozen periods.

### Frozen periods and revisions

- A frozen invoice is a promise. Replay never rewrites `frozen_invoices[]`.
  If recomputation differs, a `revisions[]` entry carries the delta, the
  recomputed line items (with their source events and contract versions), and
  `status: "pending"`.
- Only periods with **evidence** in the batch are recomputed. Replaying
  February alone cannot manufacture a January revision; a frozen period with
  no evidence is echoed as `frozen_not_recomputed` with a reason.
- `--emit-freeze PATH` writes receipts for the open invoices of a run; feeding
  them back as `frozen_invoices` (or `history`) is what closing a period means
  operationally.
- Contract revisions after a freeze therefore cannot rewrite billed history:
  they show up as a revision on the affected periods, which is exactly the
  "impact report" a finance team needs.

### Failure handling

- `on_error: "report"` (default): per-event problems become issues; the rest
  of the batch is billed and `status` is `partial` (or `failed` if nothing
  billable remained). One bad row never loses a month.
- `on_error: "fail"`: any error issue aborts the whole run with `InputError`.
  This is the settlement mode: no partial invoice is ever emitted.
- `batches[]` models producer deliveries. A batch marked failed contributes no
  events and is reported (`batch_failed`); the other batches still bill. No
  partial write exists: replay either returns the full document or raises.
- Replays are safe to retry: duplicates are idempotent, and an interrupted run
  has no side effects because the engine is pure.

## Choosing an execution model

| model | cost per run | correctness risk | when to use |
| --- | --- | --- | --- |
| Stream accumulation (per event) | O(1) per event, but needs mutable state per open period and a lock per account | Re-running is not free of side effects; late/duplicate handling and tier re-pricing are approximations; a bad retry can double-bill unless every write is idempotent | Live dashboards, near-real-time spend caps |
| Close-of-period batch replay (this engine) | O(n log n) per batch, no state to repair | Full: every invoice is derived from the whole period's evidence and can be re-derived at any time | Invoicing, credit notes, audit |
| Hybrid snapshot | Stream for the open period, freeze at close, batch replay for late evidence and revisions | Best: bounded work per run, and closed periods are immutable | Production SaaS billing at scale |

The hybrid is what this repository implements as a pure function: the
open-period run is the batch replay, freezing is `--emit-freeze`, and late
evidence produces revisions instead of rewrites. The stream layer is a
caching concern on top and cannot be the source of truth.

### Failure recovery and audit boundaries

- **Version lock**: `contract_lock_digest` + `expected_contract_lock_digest`
  make "the same terms were used" checkable before money is issued.
- **Input/output identity**: `input_digest` covers the normalized batch,
  contracts and frozen receipts; `output_digest` covers the invoices,
  revisions and status. Persisting both is enough to prove a re-run matched.
- **Per-event trail**: `audit.events[]` records every input event with its
  disposition, so a disputed line can be traced back to raw rows.
- **Freeze receipts** are digest-addressed, so they can be stored in any
  order and re-imported without normalisation.

## Modules

- `util.py` — decimal, timestamp, hashing and canonical JSON helpers.
- `models.py` — immutable records and the pinned `RULES_VERSION`.
- `periods.py` — half-open billing windows in a configured timezone.
- `parser.py` — validation, identity content-addressing, dedupe/conflict.
- `ledger.py` — retract / restore / correct / refund semantics.
- `contracts.py` — version parsing, interval selection, lock digests.
- `engine.py` — allocation, pricing, refunds, invoices, audit.
- `snapshots.py` — freeze import, frozen vs open split, revisions.
- `digests.py` — canonical invoice/output digests.
- `replay.py` — shuffled re-run determinism verification.
- `report.py` / `pipeline.py` / `cli.py` — stable envelope and entry points.

## Example

`fixtures/replay_scenario.json` exercises three accounts across three months:
a month-boundary event pair, a contract version switch, cumulative tier
crossing, fractional storage units, an idempotent duplicate, a late event, a
retraction, a same-period ratio refund and a cross-period refund.

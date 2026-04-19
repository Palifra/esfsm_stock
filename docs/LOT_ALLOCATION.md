# Lot Allocation Architecture

**Module:** `esfsm_stock` v18.0.1.8+
**Feature:** Per-lot tracking of material takes/consumes/returns on FSM jobs.
**Status:** Phase 1–4 complete on production (`eskon` DB).

---

## Why this exists

Previously, `esfsm.job.material.lot_id` was a single `Many2one(stock.lot)` —
one lot per material line. Real cable jobs often draw from multiple lots
(e.g., 78m split as 17m LOT2 + 61m LOT3). The single-lot field lost the
detail, and downstream consume/return wizards asked Odoo for the full
quantity of the remembered lot → negative-stock errors.

Related GitHub issues:
- [#1] Take wizard не синхронизира lot_id назад (closed)
- [#2] Consume/return wizards: lot_id readonly без UI (closed)
- [#3] `_handle_lot_tracking` третира празен lot_id како 'untracked' (closed)
- [#4] `material.lot_id` Many2one не поддржува multi-lot takes (closed)
- [#8] Consume blocker: multi-lot take leaves non-primary lot unusable (closed)

---

## Data model

```
esfsm.job.material  (ledger per job × material line)
  ├── id, job_id, product_id, product_uom_id
  ├── planned_qty, taken_qty, used_qty, returned_qty   (scalar totals)
  ├── lot_id                        (legacy Many2one, deprecated — Phase 5 drop)
  ├── lot_id_legacy_archive         (JSONB — snapshot pre-migration, for rollback)
  ├── primary_lot_id  (computed)    (largest allocation by qty, for API v2 compat)
  ├── manual_lot_selection          (Boolean — reserved for Phase 5+ manual UX)
  ├── lot_allocation_historical_gap (Boolean — skips sum_check for legacy rows)
  ├── taken_qty_per_lot_sum         (computed — sum of allocation.taken_qty)
  ├── used_qty_per_lot_sum          (computed)
  ├── returned_qty_per_lot_sum      (computed)
  └── lot_allocation_ids            (One2many to sub-model)

esfsm.job.material.lot  (NEW Phase 1 — one row per material × lot pairing)
  ├── material_id (FK, ondelete=cascade, indexed)
  ├── lot_id      (FK, ondelete=restrict, indexed)
  ├── taken_qty, used_qty, returned_qty  (per-lot scalars)
  ├── available_to_consume_qty   (computed, stored — taken - used - returned)
  ├── available_to_return_qty    (computed, stored)
  └── UNIQUE(material_id, lot_id)

stock.picking
  └── esfsm_allocation_synced  (Boolean — idempotency flag set after sync)
```

### Invariants

1. `sum(lot_allocation_ids.X_qty) == material.X_qty` for X in (taken, used,
   returned) **unless** `lot_allocation_historical_gap=True`, enforced by
   `_check_lot_sum_matches` (suspendable via `skip_allocation_sum_check`
   context during multi-step dual-write transactions).

2. `taken_qty >= used_qty + returned_qty` on every allocation
   (per-lot `_check_used_quantity`, `_check_returned_quantity` using
   `float_compare` with UoM rounding).

3. `UNIQUE(material_id, lot_id)` — one allocation row per (material, lot)
   pair. Concurrent-write races handled via
   `_get_or_create_allocation` savepoint + retry.

4. Any material is in exactly ONE of:
   - Allocations present (`lot_allocation_ids != []`)
   - Historical gap (`lot_allocation_historical_gap = True`)
   - Untouched (untracked product, no take, or pre-migration)

---

## Feature flag

`ir.config_parameter` key:
`esfsm_stock.per_lot_allocations_enabled` (string `'True'` / `'False'`,
default `False`). When OFF, wizards run legacy-only paths and sync helpers
are inert. Enable via **Settings → ESFSM Stock**.

Reading pattern — read ONCE per wizard action, propagate via kwarg:

```python
per_lot = self.env['esfsm.job.material']._is_per_lot_enabled()
for line in lines:
    material._sync_allocation_on_take(picking, per_lot_enabled=per_lot)
```

Never re-read mid-loop — prevents mid-transaction inconsistency if an
admin flips the setting while a wizard is open.

---

## Dual-write helpers (Phase 2)

All on `esfsm.job.material`, all no-ops when `per_lot_enabled=False` or
`product_tracking='none'`.

| Method | Called from | Effect |
|--------|-------------|--------|
| `_sync_allocation_on_take(picking, per_lot_enabled=None)` | Take wizard | Mirrors `picking.move_line_ids.lot_id` distribution into allocations; idempotent via `picking.esfsm_allocation_synced`; re-validates sums on exit |
| `_sync_allocation_on_take_explicit(lot, qty, per_lot_enabled=None)` | Add wizard | Creates/increments one allocation for the user-picked lot |
| `_sync_allocation_on_consume(qty, lot=None, per_lot_enabled=None)` | Consume wizard (Phase 2 legacy path) | Distributes consume delta across allocations via FEFO |
| `_sync_allocation_on_return(qty, lot=None, per_lot_enabled=None)` | Return wizard (Phase 2 legacy path) | Same pattern for returns |
| `_distribute_across_allocations(total, field, available_field, lot=None)` | Internal | Plan-then-apply FEFO loop — Python snapshot before any writes |
| `_get_or_create_allocation(lot, initial_qty=0.0)` | Internal | Upsert with UniqueViolation recovery inside a savepoint |
| `_validate_allocation_sums()` | Internal | Post-sync integrity check with normal context |

### FEFO ordering

`_fefo_sort_key(alloc)` returns `(expiration_date, create_date, id)`:

- If `product_expiry` is installed, earliest-expiring lot first.
- Otherwise, earliest-created lot first (same as FIFO by stock.lot.create_date).
- Allocation `id` as deterministic tie-breaker.

---

## Phase 4 wizard flow (UI cutover)

**Consume / Return wizards:**

- `default_get` produces ONE wizard line per `(material, allocation)` pair.
- User enters `consume_qty` / `return_qty` per row.
- `action_confirm` updates `allocation.X_qty += line.qty` per row, then
  rebuilds `material.X_qty = sum(allocations.X_qty)` — allocation is the
  source of truth, material is the derived aggregate.
- Materials WITHOUT allocations (`lot_allocation_historical_gap` or
  untracked) get ONE row per material with legacy scalar update. Fallback
  path preserves back-compat for pre-migration data.

**Pagination:** `<field name="line_ids" limit="20">` handles long lot lists
(up to 10+ allocations per material on real cable jobs).

**API cutover:** `esfsm_api/controllers/main.py` returns both legacy
`lot_id` / `lot_name` (sourced from computed `primary_lot_id`) and new
`lot_allocations[]` array. Old mobile clients keep working; new clients
can iterate per-lot data.

---

## Drift detection

`_cron_detect_allocation_drift()` runs daily (cron
`ir_cron_detect_allocation_drift`). Scans all materials with
`lot_allocation_ids` and no `historical_gap` flag; for each, compares
`material.X_qty` against `sum(allocation.X_qty)` using `float_compare`
with UoM rounding.

Drift triggers a warning log:

```
Allocation drift detected: material=1234 job=RN/2026/00099 product=001-101-0021
taken sum=98.0 total=100.0 delta=-2.0
```

No auto-fix — surfaces data-integrity issues for manual inspection.

---

## Migration engine (Phase 3)

`esfsm.lot.allocation.migration` — AbstractModel with entry points
exposed in Settings → ESFSM Stock → Phase 3 Migration.

| Method | UI button | Effect |
|--------|-----------|--------|
| `_classify_materials()` | — | Buckets materials into clean / multi_lot / ambiguous / gap / untracked / already_migrated |
| `dry_run()` | **Dry Run** | Classify + formatted report, no writes |
| `migrate(commit=True)` | **Commit Migration** | Auto-create allocations for clean + multi_lot buckets; flag `gap` materials as historical_gap; archive legacy `lot_id` to JSON |
| `classify_ambiguous_by_shortage()` | — | Split remaining ambiguous combos by whether lot history covers taken_qty |
| `format_ambiguous_report()` | **Shortage Report** | Human-readable classification of remaining ambiguous |
| `mark_shortage_combos_as_gap()` | **Bulk Gap Shortage Combos** | Flag all shortage + no_history combos as gap |
| `mark_all_ambiguous_as_gap()` | **Bulk Gap ALL Ambiguous** | Flag every remaining ambiguous combo (historical scenario) |
| `rollback()` | **Rollback** | Restore `lot_id` from JSON archive; delete all allocations |

**Ambiguous combo:** (job_id, product_id) combination with >1 material
line for the same tracked product. Picking lot distribution can't be
automatically attributed to specific material lines.

**Resolution wizard** (`esfsm.lot.resolution.wizard`): manual UI for
distributing lot quantities across material lines, with three exit paths:

- **Resolve** (visible when `shortage=0`): create allocations per user input.
- **Mark as Gap**: flag combo's materials as historical_gap.
- **Skip**: leave unresolved for later.

---

## Rollback path

`lot_id_legacy_archive` (JSONB on every migrated material) stores:

```json
{"lot_id": 1234, "lot_name": "LOT-A-2024", "archived_at": "2026-04-19 12:34:56"}
```

`rollback()` iterates archived materials and:
1. Restores `material.lot_id` from JSON.
2. Clears `lot_allocation_historical_gap`.
3. Deletes all `lot_allocation_ids`.
4. Clears the archive field.

Reversible until Phase 5 (planned — drop legacy column + rollback method).

---

## Phase rollout timeline (production)

| Phase | Date | Version | Effect |
|-------|------|---------|--------|
| 1. Schema deploy | 2026-04-19 | 18.0.1.4.0 | New model + fields + feature flag (OFF) |
| 2. Dual-write helpers | 2026-04-19 | 18.0.1.5.0 | Wizards mirror writes to allocations when flag ON |
| Code review hardening | 2026-04-19 | 18.0.1.6.0 | FIFO snapshot, post-sync validation, UniqueViolation retry, float_compare |
| Phase 3 migration engine | 2026-04-19 | 18.0.1.7.0 | Classifier, dry-run, commit, resolution wizard |
| 3.1 float_compare fix | 2026-04-19 | 18.0.1.7.1 | UoM-rounded constraint checks |
| 4. UI cutover | 2026-04-19 | 18.0.1.8.0 | Per-allocation flat list consume/return wizards + API v3 |
| 4.1 resolution wizard redesign | 2026-04-19 | 18.0.1.8.1 | Amplification fix + Gap/Skip escape hatches |
| 4.2 bulk gap shortage | 2026-04-19 | 18.0.1.8.2 | Shortage report + bulk gap for insufficient lot history |
| 4.3 bulk gap ALL | 2026-04-19 | 18.0.1.8.3 | Bulk gap for historical-only combos |
| #3 defense-in-depth | 2026-04-19 | 18.0.1.8.4 | `_handle_lot_tracking` raises on tracked + missing lot |

---

## Key files

- `models/esfsm_job_material.py` — material model + sync helpers + drift cron
- `models/esfsm_job_material_lot.py` — allocation sub-model
- `models/lot_allocation_migration.py` — Phase 3 migration engine
- `models/stock_picking.py` — `esfsm_allocation_synced` flag
- `models/res_config_settings.py` — feature flag + UI buttons
- `models/stock_picking_service.py` — `_handle_lot_tracking` (#3 fix)
- `wizards/esfsm_take_material_wizard.py` — dual-write on take
- `wizards/esfsm_consume_material_wizard.py` — per-allocation flat list
- `wizards/esfsm_return_material_wizard.py` — per-allocation flat list
- `wizards/esfsm_add_material_wizard.py` — explicit allocation on add
- `wizards/esfsm_lot_resolution_wizard.py` — ambiguous combo resolver
- `data/ir_cron_data.xml` — daily drift scan cron
- `views/esfsm_job_material_views.xml` — notebook page with allocations
- `views/esfsm_job_material_lot_views.xml` — standalone admin views
- `views/wizard_views.xml` — consume/return flat lists
- `tests/test_lot_allocation.py` — 24 unit tests (schema + sync)
- `tests/test_migration.py` — 8 unit tests (migration flows)

---

## Production state snapshot (2026-04-19)

```
Total material rows:       1280
  ├── Untracked (skipped):  924
  └── Tracked with take:    352
      ├── With allocations: 108 (129 allocation rows)
      └── Gap-flagged:      244 (62 Phase 3 auto + 182 bulk ambiguous)
Archives (reversible):      352 JSON snapshots
Drift:                      0
Feature flag:               ON
```

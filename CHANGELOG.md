# Changelog

All notable changes to `esfsm_stock`. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [18.0.1.8.4] — 2026-04-19

### Added
- Phase 3 bulk actions in Settings:
  - **Shortage Report** — classifies remaining ambiguous combos by whether
    picking lot history covers material_taken.
  - **Bulk Gap Shortage Combos** — flags only combos with shortage or no
    lot history as historical_gap.
  - **Bulk Gap ALL Ambiguous** — flags every remaining ambiguous combo
    (for fully historical scenarios).
- Resolution wizard UX: now shows total material taken, total lot qty,
  shortage delta, and hides Resolve button when shortage > 0.
- Resolution wizard escape hatches: **Mark as Gap** and **Skip** buttons.

### Fixed
- `_handle_lot_tracking` (issue #3): raises UserError when a tracked
  product's move has `lot_id=False`, instead of silently treating it as
  untracked. Defense-in-depth.
- Resolution wizard amplification bug: `_get_picking_lot_qtys` was called
  per-material inside a loop of materials sharing (job, product) — same
  query results summed N times. Fixed to call once per combo.
- `_check_used_quantity` and `_check_returned_quantity` constraints on
  both `esfsm.job.material` and `esfsm.job.material.lot` now use
  `float_compare` with UoM rounding — prevents false positives from
  IEEE-754 representation quirks (e.g., 2.43 vs 2.4299999999999997).

## [18.0.1.8.0] — 2026-04-19

### Added — Phase 4 UI cutover
- Consume wizard: per-allocation flat list — one row per (material, lot).
  Historical gap materials keep single-row legacy UX.
- Return wizard: same flat list pattern.
- API v3 `lot_allocations[]` array in material serialization; legacy
  `lot_id` / `lot_name` now sourced from computed `primary_lot_id`.
- Source-of-truth inversion: `material.X_qty = sum(allocations.X_qty)`
  after wizard writes — allocation is primary, material is derived.

## [18.0.1.7.0] — 2026-04-19

### Added — Phase 3 migration engine
- `esfsm.lot.allocation.migration` (AbstractModel) with:
  - `_classify_materials()` — buckets into clean/multi_lot/ambiguous/gap
  - `dry_run()` — preview report
  - `migrate(commit=True)` — creates allocations + gap flags +
    JSON archive
  - `rollback()` — restores from JSON archive
- `esfsm.lot.resolution.wizard` — manual UI for ambiguous combos.
- `esfsm.job.material.lot_id_legacy_archive` (JSONB) — rollback snapshot.
- Settings → ESFSM Stock buttons: Dry Run, Commit Migration, Resolve
  Ambiguous, Rollback.

## [18.0.1.6.0] — 2026-04-19

### Fixed — Code review hardening
- FIFO distribution now plan-then-apply (snapshot first, write at end).
- Post-sync `_validate_allocation_sums()` catches drift on every sync call.
- `_get_or_create_allocation()` recovers from UNIQUE race via savepoint retry.
- Daily `_cron_detect_allocation_drift` scans for mismatches.
- `_sync_allocation_on_take` requires `picking.state == 'done'`.
- Idempotent via `stock.picking.esfsm_allocation_synced` flag.
- FIFO → FEFO ordering: `(expiration_date, create_date, alloc_id)`.
- All quantity checks use `float_compare` with UoM rounding.

## [18.0.1.5.0] — 2026-04-19

### Added — Phase 2 dual-write
- `_sync_allocation_on_take(picking, per_lot_enabled=None)` helper.
- `_sync_allocation_on_take_explicit(lot, qty, per_lot_enabled=None)`.
- `_sync_allocation_on_consume(qty, lot=None, per_lot_enabled=None)`.
- `_sync_allocation_on_return(qty, lot=None, per_lot_enabled=None)`.
- `skip_allocation_sum_check` context flag for intermediate writes.
- 4 wizards integrated: take, consume, return, add.

## [18.0.1.4.0] — 2026-04-19

### Added — Phase 1 schema
- New model `esfsm.job.material.lot` — one row per (material, lot) pair.
- `esfsm.job.material` fields: `lot_allocation_ids` (One2many),
  `manual_lot_selection`, `lot_allocation_historical_gap`,
  `primary_lot_id` (computed), `taken/used/returned_qty_per_lot_sum`
  (computed).
- `_check_lot_sum_matches` constraint (suspendable via context flag).
- Feature flag `esfsm_stock.per_lot_allocations_enabled` (default OFF).
- Standalone admin views for allocation sub-model.
- Material form notebook page with paginated allocations (limit=5).

## [18.0.1.3.0] — 2026-04-19

### Added
- SQL constraints on material quantity fields (CHECK >= 0).
- Audit log in `material.write()` posting qty changes to job chatter.
- Stock availability check in Add wizard before picking creation.

## [18.0.1.2.0] and earlier

See git history.

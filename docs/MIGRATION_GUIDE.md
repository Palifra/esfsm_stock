# Per-Lot Allocation — Operator Guide

**Audience:** Odoo admin deploying `esfsm_stock` v18.0.1.8+ to an existing
database with legacy material data.

Pairs with `LOT_ALLOCATION.md` (architecture). This doc is task-oriented.

---

## Prerequisites

- `esfsm_stock` up-to-date (latest `main` with per-lot allocation work merged).
- Manual DB backup capability: `/usr/local/bin/odoo-backup.sh manual`.
- Admin access to **Settings → ESFSM Stock** block.
- `esfsm.group_esfsm_manager` group membership.

---

## Deployment flow

### 1. Install or upgrade the module

```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
module = env['ir.module.module'].search([('name', '=', 'esfsm_stock')])
module.button_immediate_upgrade()
env.cr.commit()
EOF
docker restart odoo_server
```

After upgrade, the schema exists but the feature flag is OFF. Wizards
behave exactly as before. Safe to stop here and monitor.

### 2. Verify baseline

```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
print('Allocations:', env['esfsm.job.material.lot'].search_count([]))
print('Archives:',    env['esfsm.job.material'].search_count([('lot_id_legacy_archive', '!=', False)]))
print('Gap flags:',   env['esfsm.job.material'].search_count([('lot_allocation_historical_gap', '=', True)]))
print('Flag:',        env['ir.config_parameter'].sudo().get_param('esfsm_stock.per_lot_allocations_enabled'))
EOF
```

Expected on a fresh install: all zeros, flag=`False`.

### 3. Take a backup before touching production data

```bash
sudo /usr/local/bin/odoo-backup.sh manual
```

Migration is reversible (JSON archive), but a ZIP backup is the cheap
insurance. Do NOT skip this.

### 4. Enable feature flag (optional dry-run first)

From UI: **Settings → ESFSM Stock → Алокации по лот = ON**, then Save.

Or via shell:
```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
env['ir.config_parameter'].sudo().set_param('esfsm_stock.per_lot_allocations_enabled', 'True')
env.cr.commit()
EOF
```

This alone is safe — dual-write starts, but existing records have no
allocations yet, so only NEW takes/consumes/returns (after this point)
write both scalars and allocations.

### 5. Run Phase 3 dry-run

**Settings → ESFSM Stock → Phase 3 Migration → Dry Run**

Alternative shell:
```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
result = env['esfsm.lot.allocation.migration'].dry_run()
print(result['report'])
EOF
```

Read the report. Check for obvious anomalies:
- `tracked_total` should match your known count of tracked materials.
- `untracked` should be much larger (consumables dominate).
- `ambiguous` count — if unexpectedly high, investigate before committing.

### 6. Commit Phase 3 migration

**Settings → ESFSM Stock → Phase 3 Migration → Commit Migration**
(confirmation prompt required).

Effect:
- Creates allocations for clean (1:1) and multi-lot (unambiguous) buckets.
- Sets `historical_gap=True` on gap materials.
- Archives legacy `lot_id` to JSON on every migrated row.
- Does NOT touch ambiguous combos (those require manual resolution).

### 7. Handle ambiguous combos

**Settings → ESFSM Stock → Phase 3 Migration → Shortage Report**

This classifies remaining ambiguous combos into 4 buckets:
- **Resolvable EXACT** — sum of lot qtys equals sum of material_taken
- **Resolvable SURPLUS** — lot qtys exceed material_taken
- **Shortage** — lot qtys below material_taken
- **No lot history** — picking had no lot data

Strategy depends on your data:

**Case A: Historical data with no intent of lot tracking**
(cables were tracked after-the-fact; pickings have incidental lots):
→ Click **Bulk Gap ALL Ambiguous**. All remaining combos are flagged as
historical_gap in one action.

**Case B: Real lot tracking exists but some combos are noise**
→ Click **Bulk Gap Shortage Combos** first (19 of the ESKON 50 were
shortage-only). Then manually resolve the rest via **Resolve Ambiguous**.

**Case C: Resolve everything manually**
→ Click **Resolve Ambiguous** and iterate through each combo. For each:
- If `shortage = 0` → distribute lot qtys across materials so each material
  row's sum equals its `taken_qty`. Click **Resolve**.
- If `shortage > 0` → the **Resolve** button is hidden. Click **Mark as Gap**.
- **Skip** to defer a combo.

### 8. Verify integrity after migration

```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
drift = env['esfsm.job.material']._cron_detect_allocation_drift()
print(f'Drift count: {drift}  (expect 0)')

# Invariant: materials with BOTH allocations AND gap flag
both = env['esfsm.job.material'].search_count([
    ('lot_allocation_ids', '!=', False),
    ('lot_allocation_historical_gap', '=', True),
])
print(f'Materials with both alloc+gap: {both}  (expect 0)')
EOF
```

---

## Monitoring

After activation, a cron runs daily at ~00:30 (default Odoo cron schedule
for daily jobs):

- Job name: **ESFSM: Detect lot allocation drift**
- Model: `esfsm.job.material`
- Method: `_cron_detect_allocation_drift`

Drift is logged at WARNING level with material id, job, product, and
per-field delta. Monitor via `docker logs odoo_server | grep drift`.

A non-zero drift after the first day indicates either:
- A wizard path that bypassed sync (possible bug — file an issue).
- An admin-made direct SQL edit.
- Concurrent-write race not handled (`UniqueViolation` should already
  be covered by `_get_or_create_allocation`).

---

## Rollback

If you need to revert Phase 3 (e.g., major bug discovered):

**Settings → ESFSM Stock → Phase 3 Migration → Rollback**
(destructive confirmation prompt).

Effect:
- Restores `material.lot_id` from `lot_id_legacy_archive` on every migrated row.
- Clears `lot_allocation_historical_gap`.
- Deletes all `esfsm.job.material.lot` records.
- Clears JSON archive.

After rollback, optionally disable the feature flag to fully revert Phase 2
behavior:

```bash
docker exec -i odoo_server odoo shell -d eskon --no-http << 'EOF'
env['ir.config_parameter'].sudo().set_param('esfsm_stock.per_lot_allocations_enabled', 'False')
env.cr.commit()
EOF
```

Then optionally downgrade the module version.

**Note:** Rollback DOES NOT restore stock.picking states or move_line
data — those are native Odoo records untouched by our migration.

---

## Troubleshooting

### "Picking XXX не е валидиран (state=draft)"

`_sync_allocation_on_take` was called with a picking that hasn't been
validated. The wizard flow always validates before calling sync; direct
calls from custom code should ensure `picking.state == 'done'` first.

### "Сумата на алокации не се совпаѓа со taken_qty"

Resolution wizard validation — user's manual distribution doesn't match.
Either adjust the `Допиши` column to equal `Material taken` per row, or
use **Mark as Gap** if the shortage can't be resolved.

### "Збирот на X по лот не се совпаѓа со вкупно"

Post-sync drift detection — raised by `_check_lot_sum_matches`. Usually
means a wizard path updated `material.X_qty` but not the matching
allocations (or vice versa). If reproducible, file an issue with the
job/material/picking IDs.

### "Лот мора да се наведе за производ XYZ"

From `_handle_lot_tracking` (fix for #3). Raised when the caller passes
`lot_id=False` for a tracked product. Check the wizard line — the lot
column should be populated before clicking confirm.

### Feature flag toggles mid-session

If an admin changes the flag while a wizard is open, the wizard keeps
the value read at `default_get` time (passed as `per_lot_enabled` kwarg).
This is intentional — prevents mid-transaction inconsistency. The next
wizard invocation picks up the new value.

---

## Phase 5 (planned — post-activation stability period)

After 2+ weeks of stable ON operation with 0 drift:

- Drop `esfsm.job.material.lot_id` column.
- Remove wizard legacy paths and `lot_id_legacy_archive` field.
- Remove `rollback()` method (point of no return).
- Simplify `primary_lot_id` compute (no fallback).

Not automated — requires a follow-up branch and migration script.

---

## Support escalation

For issues not covered here:

- GitHub: https://github.com/Palifra/esfsm_stock/issues
- Architecture details: `docs/LOT_ALLOCATION.md`
- Design decisions: `docs/plans/2026-04-19-per-lot-allocation-design.md`
- Audit data: `docs/plans/2026-04-19-per-lot-allocation-audit.md` (parent repo)

# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install', 'esfsm_stock', 'lot_allocation')
class TestLotAllocation(TransactionCase):
    """Phase 1 tests — sub-model schema only.
    Wizard integration tests belong to Phase 2+."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'LotTest Customer'})
        cls.product = cls.env['product.product'].create({
            'name': 'LotTest Product',
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })
        cls.product_untracked = cls.env['product.product'].create({
            'name': 'LotTest Untracked',
            'type': 'consu',
            'is_storable': True,
            'standard_price': 20.0,
        })
        cls.lot_a = cls.env['stock.lot'].create({
            'name': 'LOT-A',
            'product_id': cls.product.id,
        })
        cls.lot_b = cls.env['stock.lot'].create({
            'name': 'LOT-B',
            'product_id': cls.product.id,
        })
        cls.lot_c = cls.env['stock.lot'].create({
            'name': 'LOT-C',
            'product_id': cls.product.id,
        })
        cls.job = cls.env['esfsm.job'].create({
            'name': 'TEST-LOT-JOB',
            'partner_id': cls.partner.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    def _material(self, **kw):
        vals = {
            'job_id': self.job.id,
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'planned_qty': 10.0,
        }
        vals.update(kw)
        return self.env['esfsm.job.material'].create(vals)

    def _alloc(self, material, lot, **kw):
        vals = {'material_id': material.id, 'lot_id': lot.id}
        vals.update(kw)
        return self.env['esfsm.job.material.lot'].create(vals)

    # ── Sub-model CRUD ──

    def test_01_create_allocation(self):
        material = self._material()
        alloc = self._alloc(material, self.lot_a, taken_qty=3.0)
        self.assertEqual(alloc.material_id, material)
        self.assertEqual(alloc.lot_id, self.lot_a)
        self.assertEqual(alloc.product_id, self.product)
        self.assertEqual(alloc.job_id, self.job)

    def test_02_available_to_consume_computed(self):
        material = self._material()
        alloc = self._alloc(material, self.lot_a, taken_qty=5.0, used_qty=2.0, returned_qty=1.0)
        self.assertEqual(alloc.available_to_consume_qty, 2.0)
        self.assertEqual(alloc.available_to_return_qty, 2.0)

    def test_03_used_cannot_exceed_taken(self):
        material = self._material()
        with self.assertRaises(ValidationError):
            self._alloc(material, self.lot_a, taken_qty=2.0, used_qty=5.0)

    def test_04_returned_cannot_exceed_available(self):
        material = self._material()
        with self.assertRaises(ValidationError):
            self._alloc(material, self.lot_a, taken_qty=3.0, used_qty=2.0, returned_qty=5.0)

    def test_05_unique_material_lot(self):
        material = self._material()
        self._alloc(material, self.lot_a, taken_qty=1.0)
        with self.assertRaises(Exception):
            # SQL UNIQUE constraint
            self._alloc(material, self.lot_a, taken_qty=2.0)

    def test_06_cascade_on_material_delete(self):
        material = self._material(taken_qty=0)
        alloc = self._alloc(material, self.lot_a, taken_qty=1.0)
        alloc_id = alloc.id
        material.unlink()
        self.assertFalse(self.env['esfsm.job.material.lot'].browse(alloc_id).exists())

    # ── Material extensions ──

    def test_10_lot_allocation_ids_one2many(self):
        material = self._material(taken_qty=10.0, used_qty=0, returned_qty=0)
        self._alloc(material, self.lot_a, taken_qty=6.0)
        self._alloc(material, self.lot_b, taken_qty=4.0)
        self.assertEqual(len(material.lot_allocation_ids), 2)
        self.assertIn(self.lot_a, material.lot_allocation_ids.mapped('lot_id'))
        self.assertIn(self.lot_b, material.lot_allocation_ids.mapped('lot_id'))

    def test_11_primary_lot_id_largest(self):
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=3.0)
        self._alloc(material, self.lot_b, taken_qty=7.0)
        self.assertEqual(material.primary_lot_id, self.lot_b)

    def test_12_primary_lot_falls_back_to_legacy(self):
        material = self._material(taken_qty=0, lot_id=self.lot_c.id)
        self.assertEqual(material.primary_lot_id, self.lot_c)

    def test_13_lot_sums_computed(self):
        material = self._material(taken_qty=10.0, used_qty=3.0, returned_qty=2.0)
        self._alloc(material, self.lot_a, taken_qty=6.0, used_qty=2.0, returned_qty=1.0)
        self._alloc(material, self.lot_b, taken_qty=4.0, used_qty=1.0, returned_qty=1.0)
        self.assertEqual(material.taken_qty_per_lot_sum, 10.0)
        self.assertEqual(material.used_qty_per_lot_sum, 3.0)
        self.assertEqual(material.returned_qty_per_lot_sum, 2.0)

    def test_14_sum_constraint_mismatch(self):
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=6.0)
        with self.assertRaises(ValidationError):
            # Material claims taken=10 but sum(allocs)=6 → mismatch
            material.invalidate_recordset(['taken_qty_per_lot_sum'])
            material._check_lot_sum_matches()

    def test_15_historical_gap_bypasses_sum(self):
        material = self._material(taken_qty=10.0, lot_allocation_historical_gap=True)
        # No allocations at all — historical gap allowed
        material._check_lot_sum_matches()  # should NOT raise
        # Sum would be 0 vs taken=10, but gap flag skips check
        self.assertFalse(material.lot_allocation_ids)

    def test_16_manual_lot_selection_default_off(self):
        material = self._material()
        self.assertFalse(material.manual_lot_selection)

    def test_17_untracked_material_no_allocation_needed(self):
        material = self.env['esfsm.job.material'].create({
            'job_id': self.job.id,
            'product_id': self.product_untracked.id,
            'product_uom_id': self.product_untracked.uom_id.id,
            'planned_qty': 5.0,
            'taken_qty': 5.0,
        })
        self.assertEqual(material.product_tracking, 'none')
        # Sum constraint skips untracked (no allocations, not gap, but empty)
        material._check_lot_sum_matches()  # should NOT raise

    # ── Primary lot computed: consistency ──

    def test_20_primary_lot_empty_when_no_alloc_no_legacy(self):
        material = self._material()
        self.assertFalse(material.primary_lot_id)

    def test_21_allocation_changes_primary_lot(self):
        """M1 fix check: primary_lot_id should auto-recompute via @api.depends
        chain when child allocations mutate — no manual invalidation needed."""
        material = self._material(taken_qty=10.0)
        alloc_a = self._alloc(material, self.lot_a, taken_qty=3.0)
        self._alloc(material, self.lot_b, taken_qty=7.0)
        self.assertEqual(material.primary_lot_id, self.lot_b)
        # Increase lot_a to overtake — no manual invalidate_recordset
        alloc_a.taken_qty = 9.0
        self.assertEqual(material.primary_lot_id, self.lot_a)

    # ── Phase 2 dual-write sync helpers ──

    def _enable_flag(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

    def _disable_flag(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')

    def test_30_flag_default_off(self):
        self._disable_flag()
        material = self._material()
        self.assertFalse(material._is_per_lot_enabled())

    def test_31_sync_take_explicit_creates_allocation(self):
        self._enable_flag()
        material = self._material(taken_qty=5.0)
        material._sync_allocation_on_take_explicit(self.lot_a, 5.0)
        self.assertEqual(len(material.lot_allocation_ids), 1)
        self.assertEqual(material.lot_allocation_ids.taken_qty, 5.0)
        self.assertEqual(material.lot_allocation_ids.lot_id, self.lot_a)

    def test_32_sync_take_explicit_increments_existing(self):
        self._enable_flag()
        material = self._material(taken_qty=8.0)
        self._alloc(material, self.lot_a, taken_qty=3.0)
        material._sync_allocation_on_take_explicit(self.lot_a, 5.0)
        self.assertEqual(len(material.lot_allocation_ids), 1)
        self.assertEqual(material.lot_allocation_ids.taken_qty, 8.0)

    def test_33_sync_consume_fefo_distribution(self):
        """Consume uses FEFO: earliest lot (expiration or create_date) first.
        Caller must update material.used_qty BEFORE sync (real wizard flow)."""
        self._enable_flag()
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=4.0)
        self._alloc(material, self.lot_b, taken_qty=6.0)
        # Wizard updates material scalar first, then sync
        material.with_context(skip_allocation_sum_check=True).used_qty = 7.0
        material._sync_allocation_on_consume(7.0)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_a].used_qty, 4.0)
        self.assertEqual(allocs[self.lot_b].used_qty, 3.0)

    def test_34_sync_consume_specific_lot(self):
        self._enable_flag()
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=4.0)
        self._alloc(material, self.lot_b, taken_qty=6.0)
        material.with_context(skip_allocation_sum_check=True).used_qty = 3.0
        material._sync_allocation_on_consume(3.0, lot=self.lot_a)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_a].used_qty, 3.0)
        self.assertEqual(allocs[self.lot_b].used_qty, 0.0)

    def test_35_sync_return_fefo(self):
        """Return uses FEFO: earliest lot first."""
        self._enable_flag()
        material = self._material(taken_qty=10.0, used_qty=2.0)
        self._alloc(material, self.lot_a, taken_qty=4.0, used_qty=1.0)
        self._alloc(material, self.lot_b, taken_qty=6.0, used_qty=1.0)
        # Wizard updates material scalar first, then sync
        material.with_context(skip_allocation_sum_check=True).returned_qty = 4.0
        material._sync_allocation_on_return(4.0)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_a].returned_qty, 3.0)
        self.assertEqual(allocs[self.lot_b].returned_qty, 1.0)

    def test_36_sync_noop_when_flag_off(self):
        self._disable_flag()
        material = self._material(taken_qty=5.0)
        material._sync_allocation_on_take_explicit(self.lot_a, 5.0)
        self.assertEqual(len(material.lot_allocation_ids), 0)

    def test_37_sync_noop_untracked(self):
        self._enable_flag()
        material = self.env['esfsm.job.material'].create({
            'job_id': self.job.id,
            'product_id': self.product_untracked.id,
            'product_uom_id': self.product_untracked.uom_id.id,
            'planned_qty': 5.0, 'taken_qty': 5.0,
        })
        material._sync_allocation_on_take_explicit(self.lot_a, 5.0)
        self.assertEqual(len(material.lot_allocation_ids), 0)

    def test_38_dual_write_end_to_end(self):
        """Simulate full take→consume→return cycle with flag ON.
        Post-sync validation should pass at each step."""
        self._enable_flag()
        material = self._material(taken_qty=0, planned_qty=10.0)
        # Take 10 (explicit allocation path)
        material.with_context(skip_allocation_sum_check=True).taken_qty = 10.0
        material._sync_allocation_on_take_explicit(self.lot_a, 10.0)
        # Consume 6
        material.with_context(skip_allocation_sum_check=True).used_qty = 6.0
        material._sync_allocation_on_consume(6.0)
        # Return 4
        material.with_context(skip_allocation_sum_check=True).returned_qty = 4.0
        material._sync_allocation_on_return(4.0)
        # Verify final state
        alloc = material.lot_allocation_ids
        self.assertEqual(len(alloc), 1)
        self.assertEqual(alloc.taken_qty, 10.0)
        self.assertEqual(alloc.used_qty, 6.0)
        self.assertEqual(alloc.returned_qty, 4.0)
        # Constraint should pass without skip flag
        material._check_lot_sum_matches()

    def test_39_sync_raises_on_drift(self):
        """Post-sync validation: if sync leaves drift, ValidationError is raised."""
        self._enable_flag()
        material = self._material(taken_qty=10.0)  # material claims 10 taken
        # Sync only 5 → sum would be 5, material says 10 → drift
        with self.assertRaises(ValidationError):
            material._sync_allocation_on_take_explicit(self.lot_a, 5.0)

    def test_40_idempotent_take_on_retry(self):
        """M3 fix: _sync_allocation_on_take with same picking (state=done,
        synced flag set) must be a no-op on second call."""
        self._enable_flag()
        # Fake a minimal "done" picking — just enough for idempotency check
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.env.ref('stock.picking_type_internal').id,
            'location_id': self.env.ref('stock.stock_location_stock').id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
        })
        # Manually simulate a validated synced picking
        picking.sudo().write({
            'esfsm_allocation_synced': True,
        })
        # Force state=done via bypass (test-only)
        self.env.cr.execute(
            "UPDATE stock_picking SET state='done' WHERE id = %s",
            (picking.id,),
        )
        picking.invalidate_recordset(['state'])
        material = self._material(taken_qty=5.0)
        # Add prior allocation (simulating first sync)
        self._alloc(material, self.lot_a, taken_qty=5.0)
        # Second sync attempt — must not double-increment
        material._sync_allocation_on_take(picking)
        self.assertEqual(material.lot_allocation_ids.taken_qty, 5.0)

    def test_41_sync_rejects_non_done_picking(self):
        """C3 fix: sync on non-validated picking must raise."""
        self._enable_flag()
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.env.ref('stock.picking_type_internal').id,
            'location_id': self.env.ref('stock.stock_location_stock').id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
        })
        # state defaults to 'draft'
        material = self._material(taken_qty=5.0)
        with self.assertRaises(ValidationError):
            material._sync_allocation_on_take(picking)

    def test_42_unique_constraint_race_handled(self):
        """M5 fix: _get_or_create_allocation recovers from UNIQUE race.
        Simulate by pre-creating an allocation, then re-requesting."""
        self._enable_flag()
        material = self._material(taken_qty=6.0)
        # First call creates
        alloc1 = material._get_or_create_allocation(self.lot_a, initial_qty=3.0)
        self.assertEqual(alloc1.taken_qty, 3.0)
        # Second call returns existing (no UNIQUE violation crash)
        alloc2 = material._get_or_create_allocation(self.lot_a, initial_qty=0.0)
        self.assertEqual(alloc1, alloc2)
        self.assertEqual(len(material.lot_allocation_ids), 1)

    def test_43_drift_detection_cron(self):
        """C2 fix: cron detects sum mismatch and reports count."""
        self._enable_flag()
        # Clean material with matched sums
        m_clean = self._material(taken_qty=5.0)
        self._alloc(m_clean, self.lot_a, taken_qty=5.0)
        # Introduce drift by bypassing all guards
        self.env.cr.execute(
            "UPDATE esfsm_job_material_lot SET taken_qty = 3 WHERE id = %s",
            (m_clean.lot_allocation_ids.id,),
        )
        m_clean.lot_allocation_ids.invalidate_recordset()
        m_clean.invalidate_recordset()
        drift_count = self.env['esfsm.job.material']._cron_detect_allocation_drift()
        self.assertGreaterEqual(drift_count, 1)

    def test_44_fefo_expiration_priority(self):
        """FEFO: lot with expiration_date is consumed before lot without.
        Skipped if product_expiry module is not installed."""
        if 'expiration_date' not in self.env['stock.lot']._fields:
            self.skipTest('product_expiry module not installed')
        self._enable_flag()
        self.lot_b.expiration_date = fields.Datetime.from_string('2026-06-01 00:00:00')
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=5.0)
        self._alloc(material, self.lot_b, taken_qty=5.0)
        material._sync_allocation_on_consume(3.0)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_b].used_qty, 3.0)
        self.assertEqual(allocs[self.lot_a].used_qty, 0.0)
        self.lot_b.expiration_date = False

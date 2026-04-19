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
        material = self._material(taken_qty=10.0)
        alloc_a = self._alloc(material, self.lot_a, taken_qty=3.0)
        self._alloc(material, self.lot_b, taken_qty=7.0)
        self.assertEqual(material.primary_lot_id, self.lot_b)
        # Increase lot_a to overtake
        alloc_a.taken_qty = 9.0
        material.invalidate_recordset(['primary_lot_id'])
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

    def test_33_sync_consume_fifo_distribution(self):
        self._enable_flag()
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=4.0)
        self._alloc(material, self.lot_b, taken_qty=6.0)
        # Consume 7 → FIFO from biggest (lot_b first)
        material._sync_allocation_on_consume(7.0)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_b].used_qty, 6.0)
        self.assertEqual(allocs[self.lot_a].used_qty, 1.0)

    def test_34_sync_consume_specific_lot(self):
        self._enable_flag()
        material = self._material(taken_qty=10.0)
        self._alloc(material, self.lot_a, taken_qty=4.0)
        self._alloc(material, self.lot_b, taken_qty=6.0)
        material._sync_allocation_on_consume(3.0, lot=self.lot_a)
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_a].used_qty, 3.0)
        self.assertEqual(allocs[self.lot_b].used_qty, 0.0)

    def test_35_sync_return_fifo(self):
        self._enable_flag()
        material = self._material(taken_qty=10.0, used_qty=2.0)
        self._alloc(material, self.lot_a, taken_qty=4.0, used_qty=1.0)
        self._alloc(material, self.lot_b, taken_qty=6.0, used_qty=1.0)
        material._sync_allocation_on_return(4.0)
        # Lot_b has bigger available (5), returns 4 there first
        allocs = {a.lot_id: a for a in material.lot_allocation_ids}
        self.assertEqual(allocs[self.lot_b].returned_qty, 4.0)
        self.assertEqual(allocs[self.lot_a].returned_qty, 0.0)

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
        """Simulate full take→consume→return cycle with flag ON."""
        self._enable_flag()
        material = self._material(taken_qty=0, planned_qty=10.0)
        # Take: explicit 10 from lot_a
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
        # Constraint should now pass with normal context
        material._check_lot_sum_matches()

# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Task 5 tests — consume/return wizard silently drops a quantity.

The bug (both wizards): action_confirm handles a wizard line in three branches:
  1. ``line.allocation_id`` set            → update that specific allocation.
  2. material has NO allocations AND no    → bump the material scalar (legacy
     ``line.allocation_id``                  / historical-gap / untracked path).
  3. material HAS allocations              → rebuild scalar = sum(allocations).

A line where ``material.lot_allocation_ids`` is truthy BUT
``line.allocation_id`` is False falls through ALL three:
  - branch 1 skipped (no allocation_id),
  - branch 2 skipped (material has allocations),
  - branch 3's scalar-rebuild = sum(allocations) EXCLUDES this consume because
    nothing ever wrote it onto an allocation.
Meanwhile ``create_delivery_picking`` / ``create_return_picking`` already moved
the physical stock → the quantity is LOST and ``taken != used + returned`` drift
appears.

The fix distributes the consume/return across the material's allocations (FEFO)
via ``_sync_allocation_on_consume`` / ``_sync_allocation_on_return`` so the
subsequent scalar-rebuild reflects it correctly.

All records use unique ``ZZ_TCRD_*`` names so the suite is self-contained and
isolated from the 1500+ real rows in eskon_test.
"""

from odoo.tests import TransactionCase, tagged
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestConsumeReturnDrop(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Per-lot allocations must be ON for allocations to exist / be written.
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.stock_loc = cls.warehouse.lot_stock_id

        cls.partner = cls.env['res.partner'].create({
            'name': 'ZZ_TCRD_PARTNER',
        })
        cls.product = cls.env['product.product'].create({
            'name': 'ZZ_TCRD_PRODUCT',
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })
        cls.rounding = cls.product.uom_id.rounding

    @classmethod
    def tearDownClass(cls):
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')
        super().tearDownClass()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_lot(self, name):
        return self.env['stock.lot'].create({
            'name': name,
            'product_id': self.product.id,
        })

    def _stock(self, location, qty, lot):
        self.env['stock.quant']._update_available_quantity(
            self.product, location, qty, lot_id=lot)

    def _new_job(self):
        # No team / employee / vehicle → _get_source_location falls back to the
        # warehouse stock location, where we place the physical stock below.
        return self.env['esfsm.job'].create({
            'name': '/',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    def _material_with_allocation(self, taken, used=0.0, returned=0.0):
        """A lot-tracked material that already HAS an allocation tying out to
        the scalar (taken/used/returned). Mirrors the post-take state."""
        lot = self._new_lot('ZZ_TCRD_LOT_%d' % self.env['stock.lot'].search_count(
            [('name', 'like', 'ZZ_TCRD_LOT_%')]))
        material = self.env['esfsm.job.material'].with_context(
            skip_allocation_sum_check=True).create({
                'job_id': self.job.id,
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'planned_qty': max(taken, 10.0),
                'taken_qty': taken,
                'used_qty': used,
                'returned_qty': returned,
                'lot_id': lot.id,
                'price_unit': self.product.standard_price,
            })
        self.env['esfsm.job.material.lot'].with_context(
            skip_allocation_sum_check=True).create({
                'material_id': material.id,
                'lot_id': lot.id,
                'taken_qty': taken,
                'used_qty': used,
                'returned_qty': returned,
            })
        material.invalidate_recordset()
        # Sanity: fixture ties out before we start.
        material._validate_allocation_sums()
        self.assertTrue(material.lot_allocation_ids)
        return material, lot

    # ------------------------------------------------------------------
    # 1. Consume wizard: allocations present, line.allocation_id = False
    # ------------------------------------------------------------------
    def test_consume_with_allocations_but_no_allocation_id_not_dropped(self):
        """Material has an allocation (taken=10) but a wizard line is built with
        ``allocation_id = False`` (the fall-through the bug drops). After
        confirm, ``used_qty`` must reflect the consume (NOT stay 0), the
        allocation must carry it, and taken == used + returned must hold."""
        self.job = self._new_job()
        material, lot = self._material_with_allocation(taken=10.0)
        # Stock at the warehouse source so the consume picking validates.
        self._stock(self.stock_loc, 10.0, lot)

        consume_qty = 4.0
        wizard = self.env['esfsm.consume.material.wizard'].create({
            'job_id': self.job.id,
        })
        # The bug repro: material HAS allocations, but the line carries no
        # allocation_id (e.g. a legacy/aggregate line that slipped through).
        self.env['esfsm.consume.material.wizard.line'].create({
            'wizard_id': wizard.id,
            'material_line_id': material.id,
            'allocation_id': False,
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'lot_id': lot.id,
            'taken_qty': 10.0,
            'already_used_qty': 0.0,
            'already_returned_qty': 0.0,
            'available_to_consume': 10.0,
            'planned_qty': material.planned_qty,
            'consume_qty': consume_qty,
        })

        wizard.action_confirm()

        material.invalidate_recordset()
        # The consumed qty must NOT be dropped.
        self.assertEqual(
            float_compare(material.used_qty, consume_qty,
                          precision_rounding=self.rounding), 0,
            'Consumed qty was DROPPED: used_qty=%s, expected %s'
            % (material.used_qty, consume_qty))
        # Allocation reflects it.
        alloc_used = sum(material.lot_allocation_ids.mapped('used_qty'))
        self.assertEqual(
            float_compare(alloc_used, consume_qty,
                          precision_rounding=self.rounding), 0,
            'Allocation used sum (%s) must reflect the consume (%s)'
            % (alloc_used, consume_qty))
        # No over-consume: used + returned <= taken; the rest is still available.
        self.assertLessEqual(
            float_compare(material.used_qty + material.returned_qty,
                          material.taken_qty,
                          precision_rounding=self.rounding), 0,
            'Over-consume: used (%s) + returned (%s) > taken (%s)'
            % (material.used_qty, material.returned_qty, material.taken_qty))
        self.assertEqual(
            float_compare(material.taken_qty - material.used_qty
                          - material.returned_qty, 6.0,
                          precision_rounding=self.rounding), 0,
            'Available after consume must be 6.0, got %s'
            % (material.taken_qty - material.used_qty - material.returned_qty))
        # Hard guard: per-lot sums tie to material scalars (no drift).
        material._validate_allocation_sums()

    # ------------------------------------------------------------------
    # 2. Return wizard: allocations present, line.allocation_id = False
    # ------------------------------------------------------------------
    def test_return_with_allocations_but_no_allocation_id_not_dropped(self):
        """Mirror of #1 for the return wizard. Material taken=10, used=4 (so 6
        is returnable). A return wizard line with ``allocation_id = False``
        must NOT be dropped: returned_qty reflects it, the allocation carries
        it, and taken == used + returned holds."""
        self.job = self._new_job()
        material, lot = self._material_with_allocation(taken=10.0, used=4.0)
        # The technician/vehicle source = warehouse here; stock the lot so the
        # return picking (vehicle → warehouse) validates.
        self._stock(self.stock_loc, 10.0, lot)

        return_qty = 6.0
        wizard = self.env['esfsm.return.material.wizard'].create({
            'job_id': self.job.id,
        })
        self.env['esfsm.return.material.wizard.line'].create({
            'wizard_id': wizard.id,
            'material_line_id': material.id,
            'allocation_id': False,
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'lot_id': lot.id,
            'available_qty': 6.0,
            'return_qty': return_qty,
        })

        wizard.action_confirm()

        material.invalidate_recordset()
        self.assertEqual(
            float_compare(material.returned_qty, return_qty,
                          precision_rounding=self.rounding), 0,
            'Returned qty was DROPPED: returned_qty=%s, expected %s'
            % (material.returned_qty, return_qty))
        alloc_returned = sum(material.lot_allocation_ids.mapped('returned_qty'))
        self.assertEqual(
            float_compare(alloc_returned, return_qty,
                          precision_rounding=self.rounding), 0,
            'Allocation returned sum (%s) must reflect the return (%s)'
            % (alloc_returned, return_qty))
        self.assertEqual(
            float_compare(material.taken_qty,
                          material.used_qty + material.returned_qty,
                          precision_rounding=self.rounding), 0,
            'Drift: taken (%s) != used (%s) + returned (%s)'
            % (material.taken_qty, material.used_qty, material.returned_qty))
        material._validate_allocation_sums()

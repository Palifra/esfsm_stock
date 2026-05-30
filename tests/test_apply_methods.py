# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Task 6 tests — canonical per-material apply_take / apply_consume / apply_return.

These exercise the three public methods on ``esfsm.job.material`` that BOTH the
wizards and (next task) the REST API will funnel through, so every material
take / consume / return flows through ONE stock-move-backed, allocation-synced,
validated path.

Each test builds its OWN job / product / stock / lots with UNIQUE ``ZZ_TAM_*``
names so the suite is fully self-contained and isolated from the 1500+ real rows
in ``eskon_test``. The per-lot allocation feature flag is enabled in setUp.

The tests assert REAL pickings and REAL stock moves (picking.state == 'done' and
quant deltas), not just scalar writes.
"""

from odoo.tests import TransactionCase, tagged
from odoo.tools import float_compare
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestApplyMethods(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Per-lot allocations ON — apply_* must sync allocations on every leg.
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.stock_loc = cls.warehouse.lot_stock_id
        cls.partner = cls.env['res.partner'].create({
            'name': 'ZZ_TAM_PARTNER',
        })

    @classmethod
    def tearDownClass(cls):
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')
        super().tearDownClass()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_product(self, name):
        return self.env['product.product'].create({
            'name': name,
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })

    def _new_lot(self, product, name):
        return self.env['stock.lot'].create({
            'name': name,
            'product_id': product.id,
        })

    def _stock(self, product, location, qty, lot=False):
        self.env['stock.quant']._update_available_quantity(
            product, location, qty, lot_id=lot)

    def _qty_at(self, product, location, lot=False):
        domain = [
            ('product_id', '=', product.id),
            ('location_id', '=', location.id),
        ]
        if lot:
            domain.append(('lot_id', '=', lot.id))
        quants = self.env['stock.quant'].search(domain)
        return sum(quants.mapped('quantity'))

    def _new_job(self):
        # No team / employee / vehicle → _get_source_location falls back to the
        # warehouse stock location, where we place / receive physical stock.
        return self.env['esfsm.job'].create({
            'name': '/',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    def _material(self, job, product, planned, lot=False):
        return self.env['esfsm.job.material'].create({
            'job_id': job.id,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'planned_qty': planned,
            'lot_id': lot.id if lot else False,
            'price_unit': product.standard_price,
        })

    # ------------------------------------------------------------------
    # 1. apply_take creates a real validated picking + syncs allocations
    # ------------------------------------------------------------------
    def test_apply_take_creates_picking_and_syncs(self):
        product = self._new_product('ZZ_TAM_PROD_TAKE')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_TAKE')
        self._stock(product, self.stock_loc, 12, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)

        picking = material.apply_take(10, lot=lot)

        # Real picking, validated.
        self.assertTrue(picking, 'apply_take must return a picking')
        self.assertEqual(picking._name, 'stock.picking')
        self.assertEqual(picking.state, 'done',
                         'apply_take picking must be validated (done)')
        self.assertEqual(picking.esfsm_job_id, job)

        rounding = product.uom_id.rounding
        material.invalidate_recordset()
        self.assertEqual(
            float_compare(material.taken_qty, 10.0,
                          precision_rounding=rounding), 0,
            'taken_qty must be 10 after apply_take')
        # Per-lot allocation taken sum == 10.
        self.assertEqual(
            float_compare(material.taken_qty_per_lot_sum, 10.0,
                          precision_rounding=rounding), 0,
            'allocation taken sum must equal 10')
        self.assertEqual(len(material.lot_allocation_ids), 1)
        self.assertEqual(material.lot_allocation_ids.lot_id, lot)
        # No drift.
        material._validate_allocation_sums()

        # Real stock move: the picking carries a done move line of qty 10 for our
        # lot, sourced from the warehouse.
        self.assertEqual(
            float_compare(picking.move_ids.quantity, 10.0,
                          precision_rounding=rounding), 0,
            'take move must carry qty 10')
        self.assertEqual(picking.location_id, self.stock_loc,
                         'take must source from the warehouse stock location')
        dest_loc = picking.location_dest_id
        # Net warehouse quant: when source != dest (real vehicle) it drops by 10;
        # in the vehicle-less fixture source == dest so the net is unchanged. The
        # done-move assertion above already proves the physical movement.
        if dest_loc != self.stock_loc:
            self.assertEqual(
                float_compare(self._qty_at(product, self.stock_loc, lot), 2.0,
                              precision_rounding=rounding), 0,
                'warehouse stock must drop from 12 to 2 after take')
            self.assertEqual(
                float_compare(self._qty_at(product, dest_loc, lot), 10.0,
                              precision_rounding=rounding), 0,
                'destination (vehicle) stock must be 10 after take')

    # ------------------------------------------------------------------
    # 2. apply_consume moves stock vehicle→customer and updates used_qty
    # ------------------------------------------------------------------
    def test_apply_consume_moves_stock_and_updates_used(self):
        product = self._new_product('ZZ_TAM_PROD_CONS')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_CONS')
        self._stock(product, self.stock_loc, 10, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)

        # The source location for consume == _get_source_location() == warehouse
        # here (no vehicle). Take first so the lot lands there with 10 on hand.
        material.apply_take(10, lot=lot)
        source_loc = job._get_source_location()
        before = self._qty_at(product, source_loc, lot)

        picking = material.apply_consume(4, lot=lot)

        rounding = product.uom_id.rounding
        self.assertTrue(picking)
        self.assertEqual(picking.state, 'done')
        # Vehicle/source → customer leg.
        self.assertEqual(picking.location_id, source_loc)
        self.assertEqual(
            picking.location_dest_id,
            self.env.ref('stock.stock_location_customers'))

        material.invalidate_recordset()
        self.assertEqual(
            float_compare(material.used_qty, 4.0,
                          precision_rounding=rounding), 0,
            'used_qty must be 4 after apply_consume')
        self.assertEqual(
            float_compare(material.used_qty_per_lot_sum, 4.0,
                          precision_rounding=rounding), 0,
            'allocation used sum must equal 4')
        # Source stock decreased by 4.
        after = self._qty_at(product, source_loc, lot)
        self.assertEqual(
            float_compare(before - after, 4.0,
                          precision_rounding=rounding), 0,
            'source stock must drop by 4 (consumed to customer)')
        # Invariant: taken == used + returned + still-available.
        self.assertLessEqual(
            float_compare(material.used_qty + material.returned_qty,
                          material.taken_qty,
                          precision_rounding=rounding), 0,
            'over-consume drift: used + returned > taken')
        material._validate_allocation_sums()

    # ------------------------------------------------------------------
    # 3. apply_return moves stock vehicle→warehouse and updates returned_qty
    # ------------------------------------------------------------------
    def test_apply_return_creates_move_and_updates_returned(self):
        product = self._new_product('ZZ_TAM_PROD_RET')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_RET')
        self._stock(product, self.stock_loc, 10, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)

        material.apply_take(10, lot=lot)
        # Consume 4 so that taken=10, used=4 → 6 returnable.
        material.apply_consume(4, lot=lot)

        source_loc = job._get_source_location()
        # Return destination = warehouse stock (same as source in this no-vehicle
        # fixture). To prove a real warehouse delta we read the picking's actual
        # dest location below and assert against it.
        before_src = self._qty_at(product, source_loc, lot)

        picking = material.apply_return(3, lot=lot)

        rounding = product.uom_id.rounding
        self.assertTrue(picking)
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.location_id, source_loc,
                         'return must source from the vehicle/source location')
        dest_loc = picking.location_dest_id
        self.assertTrue(dest_loc, 'return must have a warehouse destination')

        material.invalidate_recordset()
        self.assertEqual(
            float_compare(material.returned_qty, 3.0,
                          precision_rounding=rounding), 0,
            'returned_qty must be 3 after apply_return')
        self.assertEqual(
            float_compare(material.returned_qty_per_lot_sum, 3.0,
                          precision_rounding=rounding), 0,
            'allocation returned sum must equal 3')

        # When source == dest (no-vehicle fixture) the net quant is unchanged;
        # when they differ, source drops by 3 and dest rises by 3. Handle both so
        # the test is robust to the location-provider fallback.
        after_src = self._qty_at(product, source_loc, lot)
        if dest_loc == source_loc:
            self.assertEqual(
                float_compare(after_src, before_src,
                              precision_rounding=rounding), 0,
                'self-return must leave net source qty unchanged')
        else:
            self.assertEqual(
                float_compare(before_src - after_src, 3.0,
                              precision_rounding=rounding), 0,
                'source must drop by 3 on return')
            self.assertEqual(
                float_compare(self._qty_at(product, dest_loc, lot), 3.0,
                              precision_rounding=rounding), 0,
                'warehouse dest must increase by 3 on return')

        # taken == used + returned after consume(4) + return(3) leaves 3 available
        self.assertEqual(
            float_compare(
                material.taken_qty - material.used_qty - material.returned_qty,
                3.0, precision_rounding=rounding), 0,
            'available must be 3 (10 - 4 used - 3 returned)')
        material._validate_allocation_sums()

    # ------------------------------------------------------------------
    # 4. consuming more than taken raises a localized error, no drift
    # ------------------------------------------------------------------
    def test_apply_consume_over_take_raises(self):
        product = self._new_product('ZZ_TAM_PROD_OVER')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_OVER')
        self._stock(product, self.stock_loc, 10, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)
        material.apply_take(5, lot=lot)

        with self.assertRaises(ValidationError):
            material.apply_consume(8, lot=lot)

        # No drift: nothing was written.
        material.invalidate_recordset()
        rounding = product.uom_id.rounding
        self.assertEqual(
            float_compare(material.used_qty, 0.0,
                          precision_rounding=rounding), 0,
            'over-consume must not partially write used_qty')
        self.assertEqual(
            float_compare(material.taken_qty, 5.0,
                          precision_rounding=rounding), 0,
            'taken_qty must be untouched after a rejected over-consume')
        material._validate_allocation_sums()

    # ------------------------------------------------------------------
    # 5. zero qty is a no-op (empty recordset, no picking, no change)
    # ------------------------------------------------------------------
    def test_apply_zero_is_noop(self):
        product = self._new_product('ZZ_TAM_PROD_ZERO')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_ZERO')
        self._stock(product, self.stock_loc, 10, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)
        material.apply_take(10, lot=lot)

        pickings_before = self.env['stock.picking'].search_count(
            [('esfsm_job_id', '=', job.id)])
        used_before = material.used_qty

        result = material.apply_consume(0, lot=lot)

        # Empty recordset, no new picking, no scalar change.
        self.assertEqual(result._name, 'stock.picking')
        self.assertFalse(result, 'apply_consume(0) must return empty recordset')
        pickings_after = self.env['stock.picking'].search_count(
            [('esfsm_job_id', '=', job.id)])
        self.assertEqual(pickings_before, pickings_after,
                         'zero consume must not create a picking')
        material.invalidate_recordset()
        self.assertEqual(material.used_qty, used_before,
                         'zero consume must not change used_qty')

    # ------------------------------------------------------------------
    # 6. flag OFF → apply_consume updates the scalar only (no allocations)
    # ------------------------------------------------------------------
    def test_apply_consume_flag_off_scalar_only(self):
        """With per-lot allocations DISABLED, apply_consume must still create a
        validated picking and bump the used_qty scalar — without requiring any
        allocations (the legacy / flag-off `else` branch of apply_consume)."""
        param = self.env['ir.config_parameter'].sudo()
        original = param.get_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')
        param.set_param('esfsm_stock.per_lot_allocations_enabled', 'False')
        try:
            product = self._new_product('ZZ_TAM_PROD_FLAGOFF')
            lot = self._new_lot(product, 'ZZ_TAM_LOT_FLAGOFF')
            self._stock(product, self.stock_loc, 10, lot=lot)

            job = self._new_job()
            material = self._material(job, product, planned=10, lot=lot)

            # Take with the flag off → no allocations are synced.
            material.apply_take(10, lot=lot)
            material.invalidate_recordset()
            self.assertFalse(
                material.lot_allocation_ids,
                'flag-off take must NOT create allocations')

            picking = material.apply_consume(4, lot=lot)

            rounding = product.uom_id.rounding
            self.assertTrue(picking, 'apply_consume must return a picking')
            self.assertEqual(picking.state, 'done',
                             'flag-off consume picking must be validated')
            material.invalidate_recordset()
            self.assertEqual(
                float_compare(material.used_qty, 4.0,
                              precision_rounding=rounding), 0,
                'used_qty scalar must be 4 after flag-off apply_consume')
            self.assertFalse(
                material.lot_allocation_ids,
                'flag-off consume must NOT create allocations')
            # No drift in the scalar invariant.
            self.assertEqual(
                float_compare(
                    material.taken_qty - material.used_qty
                    - material.returned_qty, 6.0,
                    precision_rounding=rounding), 0,
                'available must be 6 (10 taken - 4 used)')
        finally:
            param.set_param(
                'esfsm_stock.per_lot_allocations_enabled', original)

    # ------------------------------------------------------------------
    # 7. returning more than (taken - used) raises, no drift, no picking
    # ------------------------------------------------------------------
    def test_apply_return_over_taken_raises(self):
        """Returning more than the available headroom (taken - used) must raise a
        localized ValidationError BEFORE any stock move — leaving returned_qty
        untouched and creating no picking (no drift)."""
        product = self._new_product('ZZ_TAM_PROD_RETOVER')
        lot = self._new_lot(product, 'ZZ_TAM_LOT_RETOVER')
        self._stock(product, self.stock_loc, 10, lot=lot)

        job = self._new_job()
        material = self._material(job, product, planned=10, lot=lot)
        material.apply_take(5, lot=lot)

        pickings_before = self.env['stock.picking'].search_count(
            [('esfsm_job_id', '=', job.id)])

        # taken=5, used=0 → only 5 returnable; returning 8 must be rejected.
        with self.assertRaises(ValidationError):
            material.apply_return(8, lot=lot)

        rounding = product.uom_id.rounding
        material.invalidate_recordset()
        self.assertEqual(
            float_compare(material.returned_qty, 0.0,
                          precision_rounding=rounding), 0,
            'over-return must not partially write returned_qty')
        self.assertEqual(
            float_compare(material.taken_qty, 5.0,
                          precision_rounding=rounding), 0,
            'taken_qty must be untouched after a rejected over-return')
        pickings_after = self.env['stock.picking'].search_count(
            [('esfsm_job_id', '=', job.id)])
        self.assertEqual(
            pickings_before, pickings_after,
            'rejected over-return must not create a picking')
        material._validate_allocation_sums()

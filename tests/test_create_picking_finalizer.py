# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestCreatePickingFinalizer(TransactionCase):
    """End-to-end + unit tests for the _create_picking_with_moves finalizer
    (now extracted as _finalize_picking_quantities).

    Two production defects are guarded here:

    (A) Multi-lot inflation: when a move is reserved across several lots
        (K move lines) and some lines carry zero quantity, the old finalizer
        forced the FULL move demand onto EVERY unfilled line, yielding total
        done = K * demand instead of demand. The fix distributes only the
        REMAINING demand.

    (B) Negative vehicle stock: for consume (Испратница) / return (Повратница)
        pickings the source is an internal vehicle/technician location. The
        old finalizer forced full demand with no availability check, driving
        vehicle stock negative. The fix raises a localized UserError when the
        available qty at an internal source is below the demand.

    Fully self-contained: every product / lot / location / job is created with
    a UNIQUE ``ZZ_TCPF_*`` name so it never collides with the 1500+ real rows
    in eskon_test.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['esfsm.stock.picking.service']
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.stock_loc = cls.warehouse.lot_stock_id

        cls.partner = cls.env['res.partner'].create({
            'name': 'ZZ_TCPF_PARTNER',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_lot_product(self, name):
        return self.env['product.product'].create({
            'name': name,
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })

    def _new_plain_product(self, name):
        return self.env['product.product'].create({
            'name': name,
            'type': 'consu',
            'is_storable': True,
            'tracking': 'none',
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

    def _new_internal_location(self, name):
        return self.env['stock.location'].create({
            'name': name,
            'usage': 'internal',
            'location_id': self.stock_loc.location_id.id or self.stock_loc.id,
        })

    def _new_job(self):
        """Minimal esfsm.job. company_id + partner_id are the only requireds
        relevant to the picking service; name defaults via sequence ('/')."""
        return self.env['esfsm.job'].create({
            'name': '/',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
        })

    def _wizard_line(self, job, material_line, product, qty, available, lot=False):
        """A real esfsm.take.material.wizard.line so create_reverse_picking's
        duck-typed attribute access (take_qty, lot_id, ...) is genuine.

        ``available`` must be >= ``qty`` or the wizard line's own
        ``_check_take_qty`` constraint rejects the row (take_qty > available_qty).
        """
        wizard = self.env['esfsm.take.material.wizard'].create({
            'job_id': job.id,
        })
        return self.env['esfsm.take.material.wizard.line'].create({
            'wizard_id': wizard.id,
            'material_line_id': material_line.id,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'take_qty': qty,
            'available_qty': available,
            'qty_to_take': qty,
            'planned_qty': qty,
            'status': 'ok',
            'lot_id': lot.id if lot else False,
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
    # Test 1 — multi-lot end-to-end: total done == demand (NOT K*demand)
    # ------------------------------------------------------------------
    def test_multilot_take_total_done_equals_demand(self):
        """Lot-tracked product split across TWO lots (6 + 4) in the warehouse.

        Run the REAL create_reverse_picking flow for demand 10 with no caller
        lot, so action_assign auto-reserves across BOTH lots -> a genuine
        2-line reservation. The validated move's total done quantity must equal
        10 exactly, never K*demand. This is the end-to-end proof that the
        finalizer keeps per-lot sums honest.
        """
        product = self._new_lot_product('ZZ_TCPF_E2E_PROD')
        lot_a = self._new_lot(product, 'ZZ_TCPF_E2E_LOTA')
        lot_b = self._new_lot(product, 'ZZ_TCPF_E2E_LOTB')
        self._stock(product, self.stock_loc, 6, lot=lot_a)
        self._stock(product, self.stock_loc, 4, lot=lot_b)

        job = self._new_job()
        material = self._material(job, product, planned=10)
        # No caller lot: forces Odoo to split the reservation across both lots.
        wl = self._wizard_line(job, material, product, qty=10,
                               available=10, lot=False)

        picking = self.service.create_reverse_picking(job, wl)

        move = picking.move_ids
        self.assertEqual(len(move), 1, "Expected a single move for the product")
        rounding = move.product_uom.rounding

        # PROOF the multi-lot reservation was REAL (>= 2 lines). If this is 1,
        # the test is not exercising the multi-lot path and must be reworked.
        self.assertGreaterEqual(
            len(move.move_line_ids), 2,
            "End-to-end test must produce a multi-line reservation to prove "
            "the finalizer does not inflate across lots")

        total_done = sum(move.move_line_ids.mapped('quantity'))
        self.assertEqual(
            float_compare(total_done, 10.0, precision_rounding=rounding), 0,
            "Total done quantity must equal demand (10), not K*demand. "
            "Got %s across %s lines." % (total_done, len(move.move_line_ids)))
        self.assertEqual(picking.state, 'done', "Picking must be validated")
        # Both reserved lots are preserved with their genuine reserved sizes.
        self.assertEqual(
            move.move_line_ids.lot_id, lot_a | lot_b,
            "Both reserved lots must be present")

    # ------------------------------------------------------------------
    # Test 1b — direct finalizer proof: K zero-qty lines do NOT inflate
    # ------------------------------------------------------------------
    def test_finalizer_distributes_remaining_across_zero_lines(self):
        """Directly drive _finalize_picking_quantities on a move whose two
        reserved lines have been zeroed.

        This reproduces the EXACT defect: with two zero-qty lines and demand 10,
        the OLD inline finalizer set EACH line to 10 -> total 20 (K*demand). The
        fix must distribute only the remaining demand (10) -> total 10.
        """
        product = self._new_lot_product('ZZ_TCPF_FIN_PROD')
        lot_a = self._new_lot(product, 'ZZ_TCPF_FIN_LOTA')
        lot_b = self._new_lot(product, 'ZZ_TCPF_FIN_LOTB')
        self._stock(product, self.stock_loc, 6, lot=lot_a)
        self._stock(product, self.stock_loc, 4, lot=lot_b)
        dest = self._new_internal_location('ZZ_TCPF_FIN_DEST')

        picking = self.env['stock.picking'].create({
            'picking_type_id': self.warehouse.int_type_id.id,
            'location_id': self.stock_loc.id,
            'location_dest_id': dest.id,
        })
        move = self.env['stock.move'].create({
            'name': 'ZZ_TCPF_FIN_MOVE',
            'product_id': product.id,
            'product_uom_qty': 10,
            'product_uom': product.uom_id.id,
            'picking_id': picking.id,
            'location_id': self.stock_loc.id,
            'location_dest_id': dest.id,
        })
        picking.action_confirm()
        picking.action_assign()
        # Reservation produced two lines (6 + 4). Zero them to mimic the state
        # the finalizer is responsible for filling.
        self.assertEqual(len(move.move_line_ids), 2,
                         "Setup must reserve two lines across two lots")
        for ml in move.move_line_ids:
            ml.quantity = 0.0
        rounding = move.product_uom.rounding

        # Source here is the warehouse (internal) and holds 10 -> guard passes.
        self.service._finalize_picking_quantities(picking)

        total_done = sum(move.move_line_ids.mapped('quantity'))
        self.assertEqual(
            float_compare(total_done, 10.0, precision_rounding=rounding), 0,
            "Finalizer must distribute remaining demand (10), not K*demand "
            "(20). Got %s." % total_done)

    # ------------------------------------------------------------------
    # Test 2 — consume shortfall raises, does NOT go negative
    # ------------------------------------------------------------------
    def test_consume_shortfall_raises_not_negative(self):
        """Internal (vehicle) source holds 3; consume picking demands 5.

        The finalizer must raise a localized UserError instead of forcing full
        demand, and the source stock must NOT be driven negative.
        """
        product = self._new_plain_product('ZZ_TCPF_SHORT_PROD')
        vehicle_loc = self._new_internal_location('ZZ_TCPF_SHORT_VEHICLE')
        self._stock(product, vehicle_loc, 3)

        job = self._new_job()
        customer_loc = self.env.ref('stock.stock_location_customers')
        picking_type = self.service._get_picking_type(
            'Испратници', self.company.id, 'outgoing')

        material_lines = [{
            'material_line_id': False,
            'product_id': product,
            'product_uom_id': product.uom_id,
            'quantity': 5,
            'lot_id': False,
        }]

        with self.assertRaises(UserError):
            self.service._create_picking_with_moves(
                job, picking_type, vehicle_loc, customer_loc,
                material_lines, 'Испратница')

        # Re-read on-hand at the source: it must not have gone negative.
        available = product.with_context(
            location=vehicle_loc.id).qty_available
        self.assertGreaterEqual(
            available, 0.0,
            "Vehicle source stock must not be driven negative")
        self.assertEqual(
            float_compare(available, 3.0, precision_rounding=product.uom_id.rounding), 0,
            "Source stock must remain untouched (3) after the rejected consume")

    # ------------------------------------------------------------------
    # Test 3 — sufficient stock validates cleanly
    # ------------------------------------------------------------------
    def test_sufficient_stock_validates(self):
        """Internal source holds 5; consume picking demands 5 -> validates,
        done qty == demand, no error."""
        product = self._new_plain_product('ZZ_TCPF_OK_PROD')
        vehicle_loc = self._new_internal_location('ZZ_TCPF_OK_VEHICLE')
        self._stock(product, vehicle_loc, 5)

        job = self._new_job()
        customer_loc = self.env.ref('stock.stock_location_customers')
        picking_type = self.service._get_picking_type(
            'Испратници', self.company.id, 'outgoing')

        material_lines = [{
            'material_line_id': False,
            'product_id': product,
            'product_uom_id': product.uom_id,
            'quantity': 5,
            'lot_id': False,
        }]

        picking = self.service._create_picking_with_moves(
            job, picking_type, vehicle_loc, customer_loc,
            material_lines, 'Испратница')

        self.assertEqual(picking.state, 'done', "Picking must be validated")
        move = picking.move_ids
        rounding = move.product_uom.rounding
        total_done = sum(move.move_line_ids.mapped('quantity'))
        self.assertEqual(
            float_compare(total_done, 5.0, precision_rounding=rounding), 0,
            "Done quantity must equal demand (5)")

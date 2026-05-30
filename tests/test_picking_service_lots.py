# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestHandleLotTracking(TransactionCase):
    """Regression tests for esfsm.stock.picking.service._handle_lot_tracking.

    Guards against the multi-lot inflation bug: when action_assign() reserves
    a move across several lots (K move lines), the old code overwrote every
    line's quantity with the FULL demand, yielding done qty = K * demand and
    destroying genuine per-lot identity.

    Fully self-contained: builds its own product, lots and stock with UNIQUE
    names so it never collides with existing eskon_test rows.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service = cls.env['esfsm.stock.picking.service']
        cls.stock_loc = cls.env.ref('stock.stock_location_stock')
        # Distinct internal destination so reservation does not short-circuit.
        cls.dest_loc = cls.env['stock.location'].create({
            'name': 'ZZ_THLT_DEST_LOC',
            'usage': 'internal',
            'location_id': cls.stock_loc.location_id.id or cls.stock_loc.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'ZZ_THLT_LOTPROD',
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })

    def _new_lot(self, name):
        return self.env['stock.lot'].create({
            'name': name,
            'product_id': self.product.id,
        })

    def _make_move(self, qty):
        move = self.env['stock.move'].create({
            'name': 'ZZ_THLT_MOVE',
            'product_id': self.product.id,
            'product_uom_qty': qty,
            'product_uom': self.product.uom_id.id,
            'location_id': self.stock_loc.id,
            'location_dest_id': self.dest_loc.id,
        })
        move._action_confirm()
        move._action_assign()
        return move

    def test_single_line_gets_lot_and_full_qty(self):
        """One lot with enough stock -> single reserved line gets the caller
        lot stamped and the full demand quantity."""
        lot_a = self._new_lot('ZZ_THLT_SINGLE_LOTA')
        self.env['stock.quant']._update_available_quantity(
            self.product, self.stock_loc, 5, lot_id=lot_a)

        move = self._make_move(5)
        # Precondition: exactly one reserved line.
        self.assertEqual(len(move.move_line_ids), 1,
                         "Expected a single reserved move line for one lot")

        self.service._handle_lot_tracking(move, lot_a)

        self.assertEqual(move.move_line_ids.lot_id, lot_a,
                         "Single line must carry the caller lot")
        self.assertEqual(sum(move.move_line_ids.mapped('quantity')), 5.0,
                         "Single line must carry the full demand quantity")

    def test_multi_line_quantities_sum_to_demand_not_n_times(self):
        """Stock split across TWO lots (6 + 4) -> action_assign reserves two
        move lines. _handle_lot_tracking must NOT inflate: the line quantities
        must still sum to the demand (10), never K * demand (20)."""
        lot_a = self._new_lot('ZZ_THLT_MULTI_LOTA')
        lot_b = self._new_lot('ZZ_THLT_MULTI_LOTB')
        self.env['stock.quant']._update_available_quantity(
            self.product, self.stock_loc, 6, lot_id=lot_a)
        self.env['stock.quant']._update_available_quantity(
            self.product, self.stock_loc, 4, lot_id=lot_b)

        move = self._make_move(10)
        # Precondition: the multi-lot reservation is REAL (two lines).
        self.assertEqual(len(move.move_line_ids), 2,
                         "Expected two reserved move lines across two lots")

        # Production callers always pass a (single) caller lot into the
        # multi-line path (the falsy-lot guard raises a UserError otherwise).
        # The OLD code overwrote BOTH lines to this lot AND set each line's
        # quantity to the full demand -> sum 20. The fix must keep sum == 10.
        self.service._handle_lot_tracking(move, lot_a)

        self.assertEqual(
            sum(move.move_line_ids.mapped('quantity')), 10.0,
            "Multi-lot line quantities must sum to demand (10), not K*demand")
        # Per-lot identity preserved (both reserved lots still present, the
        # caller lot must NOT clobber the second reserved lot).
        self.assertEqual(
            move.move_line_ids.lot_id, lot_a | lot_b,
            "Both reserved lots must be preserved, not overwritten")

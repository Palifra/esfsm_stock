# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestTakeDoubleCount(TransactionCase):
    """Regression: take-sync double-count when one job holds TWO distinct
    esfsm.job.material lines for the SAME product.

    The take wizard creates ONE Реверс picking for ALL wizard lines
    (create_reverse_picking). Each material then runs
    _sync_allocation_on_take(picking). The OLD sync attributed move lines to a
    material by PRODUCT only, so BOTH materials read ALL of the product's move
    lines in the shared picking and summed the COMBINED qty. Each material's
    allocation got the full combined qty -> _validate_allocation_sums() raises
    (lot-tracked) and the whole take rolls back.

    The fix attributes each move to its originating material line via
    stock.move.esfsm_material_line_id, so each material only sees its own moves.

    Fully self-contained: every product / lot / location / job is created with a
    UNIQUE ``ZZ_TTDC_*`` name so it never collides with real rows in eskon_test.
    The per-lot allocation feature flag is enabled in setUp because the
    double-count only manifests (as a hard ValidationError) with allocations on.
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
            'name': 'ZZ_TTDC_PARTNER',
        })
        # Per-lot allocation feature flag — the double-count surfaces as a
        # ValidationError only when allocations are written (Phase 2+).
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

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

    def _new_lot(self, product, name):
        return self.env['stock.lot'].create({
            'name': name,
            'product_id': product.id,
        })

    def _stock(self, product, location, qty, lot=False):
        self.env['stock.quant']._update_available_quantity(
            product, location, qty, lot_id=lot)

    def _new_job(self):
        return self.env['esfsm.job'].create({
            'name': '/',
            'partner_id': self.partner.id,
            'company_id': self.company.id,
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

    def _wizard_line(self, wizard, material_line, product, take_qty, available,
                     lot=False):
        """A real esfsm.take.material.wizard.line carrying material_line_id so
        create_reverse_picking's attribute access is genuine. ``available`` must
        be >= ``take_qty`` or the line's own _check_take_qty rejects it."""
        return self.env['esfsm.take.material.wizard.line'].create({
            'wizard_id': wizard.id,
            'material_line_id': material_line.id,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'take_qty': take_qty,
            'available_qty': available,
            'qty_to_take': take_qty,
            'planned_qty': take_qty,
            'status': 'ok',
            'lot_id': lot.id if lot else False,
        })

    # ------------------------------------------------------------------
    # The double-count regression
    # ------------------------------------------------------------------
    def test_two_material_lines_same_product_single_take(self):
        """ONE job, ONE lot-tracked product, TWO material lines (planned 4 + 6),
        a SINGLE take of both in one picking.

        Replicates the real take wizard action_confirm sequence exactly:
        create_reverse_picking(job, both_wizard_lines) -> one shared picking,
        then per-material write(taken_qty) + _sync_allocation_on_take(picking).

        On OLD code both materials read all move lines of the product (10 units)
        -> each allocation gets 10 while its material says 4 / 6 ->
        _validate_allocation_sums() raises ValidationError.

        On FIXED code each material only sees its own move (4 and 6) -> no error,
        taken_qty 4 and 6, allocation sums match.
        """
        product = self._new_lot_product('ZZ_TTDC_PROD')
        lot = self._new_lot(product, 'ZZ_TTDC_LOT1')
        self._stock(product, self.stock_loc, 12, lot=lot)

        job = self._new_job()
        mat_a = self._material(job, product, planned=4, lot=lot)
        mat_b = self._material(job, product, planned=6, lot=lot)

        wizard = self.env['esfsm.take.material.wizard'].create({'job_id': job.id})
        wl_a = self._wizard_line(wizard, mat_a, product, take_qty=4,
                                 available=12, lot=lot)
        wl_b = self._wizard_line(wizard, mat_b, product, take_qty=6,
                                 available=12, lot=lot)
        wizard_lines = wl_a | wl_b

        # ---- mirror EsfsmTakeMaterialWizard.action_confirm exactly ----
        picking = self.service.create_reverse_picking(job, wizard_lines)
        # Non-merge must be EXPLICIT: the _prepare_merge_moves_distinct_fields
        # override (esfsm_material_line_id) keeps the two same-product moves
        # separate through action_confirm's _merge_moves. If they ever merged,
        # one material's allocation sync would feed off the other's move-lines.
        self.assertEqual(
            len(picking.move_ids), 2,
            "two same-product material lines must produce two un-merged moves")
        per_lot = self.env['esfsm.job.material']._is_per_lot_enabled()
        self.assertTrue(per_lot, "Feature flag must be ON for this regression")

        for line in wizard_lines:
            material_ctx = line.material_line_id.with_context(
                skip_auto_picking=True,
                skip_allocation_sum_check=True,
            )
            new_taken = material_ctx.taken_qty + line.take_qty
            vals = {'taken_qty': new_taken}
            if (not material_ctx.lot_id
                    and material_ctx.product_id.tracking != 'none'):
                vals['lot_id'] = wizard._pick_primary_lot(
                    picking, material_ctx.product_id)
            material_ctx.write(vals)
            # The call that double-counted on old code:
            material_ctx._sync_allocation_on_take(picking, per_lot_enabled=per_lot)

        # ---- assertions ----
        rounding = product.uom_id.rounding
        self.assertEqual(
            float_compare(mat_a.taken_qty, 4.0, precision_rounding=rounding), 0,
            "Material A must have taken exactly its own 4, not the combined 10")
        self.assertEqual(
            float_compare(mat_b.taken_qty, 6.0, precision_rounding=rounding), 0,
            "Material B must have taken exactly its own 6, not the combined 10")
        self.assertEqual(
            float_compare(mat_a.taken_qty + mat_b.taken_qty, 10.0,
                          precision_rounding=rounding), 0,
            "Total taken across both lines must equal 10")

        # Per-lot allocation taken sums must match each material's own taken_qty
        # (this is exactly what _validate_allocation_sums enforces; it would have
        # raised on old code before we even reached here).
        self.assertEqual(
            float_compare(mat_a.taken_qty_per_lot_sum, 4.0,
                          precision_rounding=rounding), 0,
            "Material A allocation sum must equal its own taken (4)")
        self.assertEqual(
            float_compare(mat_b.taken_qty_per_lot_sum, 6.0,
                          precision_rounding=rounding), 0,
            "Material B allocation sum must equal its own taken (6)")

        # Sanity: each material has exactly one allocation, for our shared lot.
        self.assertEqual(len(mat_a.lot_allocation_ids), 1)
        self.assertEqual(len(mat_b.lot_allocation_ids), 1)
        self.assertEqual(mat_a.lot_allocation_ids.lot_id, lot)
        self.assertEqual(mat_b.lot_allocation_ids.lot_id, lot)

        # The validation that protects production must pass cleanly now.
        mat_a._validate_allocation_sums()
        mat_b._validate_allocation_sums()

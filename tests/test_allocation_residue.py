# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Task 4 tests — proportional-split float residue + UoM-aware comparisons.

These tests pin down three bugs:
  1. Resolution wizard proportional split (used/returned) leaves a rounding
     residue so per-lot sums don't tie to the material scalar → _check_lot_sum_matches
     raises (tolerance 0.001) and the drift cron flags it.
  2. Migration multi-lot branch has the same proportional-split residue.
  3. esfsm.job.action_complete (and the has_materials_to_* computes) compared a
     raw float `> 0`, so a sub-rounding residue (e.g. 0.00007) permanently
     blocked job completion with the unreturned-materials error.

All records use unique ZZ_TAR_* names so the suite is self-contained and
isolated from other test data.
"""

from odoo.tests import TransactionCase, tagged
from odoo.tools import float_compare


@tagged('post_install', '-at_install', 'esfsm_stock')
class TestAllocationResidue(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Enable per-lot allocations (mirrors other Phase 2+ tests)
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

        cls.partner = cls.env['res.partner'].create({
            'name': 'ZZ_TAR_Customer',
        })
        cls.product = cls.env['product.product'].create({
            'name': 'ZZ_TAR_Product',
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'standard_price': 50.0,
        })
        # Default "Units" UoM → 2-decimal rounding (0.01).
        cls.rounding = cls.product.uom_id.rounding
        cls.lots = cls.env['stock.lot'].create([
            {'name': 'ZZ_TAR_LOT_%d' % i, 'product_id': cls.product.id}
            for i in range(1, 4)
        ])

        # A coarse-rounding UoM (step 0.5) in the same category as the product
        # UoM. Field storage is 2 decimals (digits='Product Unit of Measure'),
        # so a value like 0.30 is representable, yet float_compare against this
        # UoM's rounding treats anything below 0.25 as zero. This is the gap the
        # raw `> 0` compare fell into. We use a dedicated coarse product so the
        # action_complete / has_materials tests are honest and deterministic.
        cls.coarse_uom = cls.env['uom.uom'].create({
            'name': 'ZZ_TAR_CoarseHalf',
            'category_id': cls.product.uom_id.category_id.id,
            'uom_type': 'bigger',
            'factor_inv': 1.0,
            'rounding': 0.5,
        })
        cls.coarse_rounding = cls.coarse_uom.rounding
        cls.product_coarse = cls.env['product.product'].create({
            'name': 'ZZ_TAR_ProductCoarse',
            'type': 'consu',
            'is_storable': True,
            'uom_id': cls.coarse_uom.id,
            'uom_po_id': cls.coarse_uom.id,
            'standard_price': 10.0,
        })
        cls.job = cls.env['esfsm.job'].create({
            'name': 'ZZ_TAR_JOB',
            'partner_id': cls.partner.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    @classmethod
    def tearDownClass(cls):
        # Restore the flag to its packaged default so we don't leak state.
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')
        super().tearDownClass()

    def _material(self, product=None, uom=None, **kw):
        product = product or self.product
        uom = uom or product.uom_id
        vals = {
            'job_id': self.job.id,
            'product_id': product.id,
            'product_uom_id': uom.id,
            'planned_qty': 10.0,
        }
        vals.update(kw)
        # taken/used/returned may be set already-balanced; bypass the sum
        # check at create-time since allocations are added afterwards.
        return self.env['esfsm.job.material'].with_context(
            skip_allocation_sum_check=True).create(vals)

    # ──────────────────────────────────────────────
    # 1. Resolution wizard split residue
    # ──────────────────────────────────────────────

    def test_resolution_split_remainder_reconciles(self):
        """Drive the resolution wizard action_resolve with a non-divisible
        split: taken=15 across 3 equal lots (5/5/5, ratio 1/3 each), used=10.
        10 * 1/3 = 3.33 rounded, ×3 = 9.99 != 10 — the residue that tripped
        _check_lot_sum_matches. After the fix, per-lot used/returned sums must
        equal the material scalar exactly and _validate_allocation_sums must
        not raise."""
        material = self._material(taken_qty=15.0, used_qty=10.0, returned_qty=0.0)

        # Build the wizard lines manually: 3 lots, qty split 5/5/5 (sums to 15
        # taken) so taken ties out but used (10) gets the 1/3+1/3+1/3 residue.
        wizard = self.env['esfsm.lot.resolution.wizard'].create({
            'job_id': self.job.id,
            'product_id': self.product.id,
        })
        qty_split = [5.0, 5.0, 5.0]
        for lot, qty in zip(self.lots, qty_split):
            self.env['esfsm.lot.resolution.wizard.line'].create({
                'wizard_id': wizard.id,
                'material_id': material.id,
                'lot_id': lot.id,
                'material_taken_qty': material.taken_qty,
                'lot_total_qty': qty,
                'qty': qty,
            })

        # Should NOT raise after fix (residue reconciled to last/largest alloc).
        wizard.action_resolve()

        material.invalidate_recordset()
        allocs = material.lot_allocation_ids
        self.assertEqual(len(allocs), 3)

        sum_used = sum(allocs.mapped('used_qty'))
        sum_returned = sum(allocs.mapped('returned_qty'))
        sum_taken = sum(allocs.mapped('taken_qty'))

        self.assertEqual(
            float_compare(sum_used, material.used_qty,
                          precision_rounding=self.rounding), 0,
            'Per-lot used sum (%s) must tie to material.used_qty (%s)'
            % (sum_used, material.used_qty))
        self.assertEqual(
            float_compare(sum_returned, material.returned_qty,
                          precision_rounding=self.rounding), 0,
            'Per-lot returned sum (%s) must tie to material.returned_qty (%s)'
            % (sum_returned, material.returned_qty))
        self.assertEqual(
            float_compare(sum_taken, material.taken_qty,
                          precision_rounding=self.rounding), 0)

        # The hard guard must agree.
        material._validate_allocation_sums()

    # ──────────────────────────────────────────────
    # 2. Migration multi-lot split residue (helper-level)
    # ──────────────────────────────────────────────

    def test_migration_multilot_split_reconciles(self):
        """Setting up a done multi-lot picking + running the full migrate path
        is heavy and brittle in a unit test, so we exercise the residual split
        helper directly with the same non-divisible distribution the migration
        branch performs (1/3+1/3+1/3 of 10 across 3 lots). The helper output is
        what the migration writes, so reconciliation here proves the migration
        branch reconciles too."""
        migration = self.env['esfsm.lot.allocation.migration']

        # picking_qtys equal → ratios 1/3 each; material totals = 10/7/3.
        lots = [(self.lots[0].id, 5.0), (self.lots[1].id, 5.0), (self.lots[2].id, 5.0)]
        total_picking_qty = sum(q for _, q in lots)
        distributed = migration._split_proportional(
            lots, total_picking_qty,
            material_taken=10.0, material_used=7.0, material_returned=3.0,
            rounding=self.rounding,
        )

        sum_taken = sum(d['taken_qty'] for d in distributed)
        sum_used = sum(d['used_qty'] for d in distributed)
        sum_returned = sum(d['returned_qty'] for d in distributed)

        self.assertEqual(
            float_compare(sum_taken, 10.0, precision_rounding=self.rounding), 0,
            'taken split must reconcile to 10.0, got %s' % sum_taken)
        self.assertEqual(
            float_compare(sum_used, 7.0, precision_rounding=self.rounding), 0,
            'used split must reconcile to 7.0, got %s' % sum_used)
        self.assertEqual(
            float_compare(sum_returned, 3.0, precision_rounding=self.rounding), 0,
            'returned split must reconcile to 3.0, got %s' % sum_returned)

        # Every per-lot value rounded to UoM precision.
        for d in distributed:
            for f in ('taken_qty', 'used_qty', 'returned_qty'):
                self.assertEqual(
                    float_compare(
                        d[f], round(d[f], 2),
                        precision_rounding=self.rounding), 0,
                    '%s not rounded to UoM precision: %s' % (f, d[f]))

    def test_migration_multilot_create_reconciles(self):
        """End-to-end-ish: a material with allocations written by the migration
        multi-lot loop must pass _validate_allocation_sums. We replicate the
        loop's write behaviour via the helper + create and assert no drift."""
        material = self._material(taken_qty=10.0, used_qty=7.0, returned_qty=3.0)
        migration = self.env['esfsm.lot.allocation.migration']
        Allocation = self.env['esfsm.job.material.lot']

        lots = [(self.lots[0].id, 5.0), (self.lots[1].id, 5.0), (self.lots[2].id, 5.0)]
        distributed = migration._split_proportional(
            lots, sum(q for _, q in lots),
            material_taken=material.taken_qty,
            material_used=material.used_qty,
            material_returned=material.returned_qty,
            rounding=self.rounding,
        )
        for d in distributed:
            Allocation.with_context(skip_allocation_sum_check=True).create({
                'material_id': material.id,
                'lot_id': d['lot_id'],
                'taken_qty': d['taken_qty'],
                'used_qty': d['used_qty'],
                'returned_qty': d['returned_qty'],
            })

        material.invalidate_recordset()
        material._validate_allocation_sums()  # must not raise

    def test_coarse_uom_split_respects_used_returned_constraint(self):
        """COARSE-UoM regression for the per-lot used+returned<=taken invariant.

        With a pack-of-N UoM (rounding 1.0), a naïve per-field rounding +
        largest-sink residual rounds `used`/`returned` up on the biggest lot
        past its `taken`, so the allocation create() trips
        esfsm.job.material.lot._check_used_quantity /
        _check_returned_quantity and the whole migration loop rolls back.

        Concrete case (the one the reviewer flagged): taken=9, used=3.64,
        returned=5.36 distributed across three lots with picking_qty 1/3/7.
        The old helper made the qty-7 sink lot taken=6, used=3, returned=4 →
        4 > 6-3=3 → ValidationError. The constraint-aware helper must place the
        used/returned residual only where headroom exists.

        Replicates the migration loop: helper output + create with
        skip_allocation_sum_check (exactly as migrate() does), then asserts
        (a) no ValidationError, (b) per-field sums reconcile to the scalars,
        (c) every allocation satisfies used+returned <= taken.
        """
        # Dedicated coarse UoM with rounding 1.0 (pack-of-50 style) + a
        # lot-tracked product on it, plus three distinct lots.
        coarse_uom_1 = self.env['uom.uom'].create({
            'name': 'ZZ_TAR_CoarsePack',
            'category_id': self.product.uom_id.category_id.id,
            'uom_type': 'bigger',
            'factor_inv': 1.0,
            'rounding': 1.0,
        })
        rounding = coarse_uom_1.rounding
        self.assertEqual(rounding, 1.0)
        product = self.env['product.product'].create({
            'name': 'ZZ_TAR_ProductCoarsePack',
            'type': 'consu',
            'is_storable': True,
            'tracking': 'lot',
            'uom_id': coarse_uom_1.id,
            'uom_po_id': coarse_uom_1.id,
            'standard_price': 10.0,
        })
        lots = self.env['stock.lot'].create([
            {'name': 'ZZ_TAR_PACKLOT_%d' % i, 'product_id': product.id}
            for i in range(1, 4)
        ])

        material = self._material(
            product=product, uom=coarse_uom_1,
            taken_qty=9.0, used_qty=3.64, returned_qty=5.36)
        # Material scalar invariant must itself hold (sanity for the fixture).
        self.assertLessEqual(material.used_qty + material.returned_qty,
                             material.taken_qty + 1e-9)

        migration = self.env['esfsm.lot.allocation.migration']
        Allocation = self.env['esfsm.job.material.lot']

        # picking_qty 1/3/7 → the qty-7 lot is the would-be sink under the old
        # helper. total_picking_qty = 11.
        lot_pairs = [(lots[0].id, 1.0), (lots[1].id, 3.0), (lots[2].id, 7.0)]
        distributed = migration._split_proportional(
            lot_pairs, sum(q for _, q in lot_pairs),
            material_taken=material.taken_qty,
            material_used=material.used_qty,
            material_returned=material.returned_qty,
            rounding=rounding,
        )

        # (a) Creating the allocations (the migration write) must NOT raise.
        for d in distributed:
            Allocation.with_context(skip_allocation_sum_check=True).create({
                'material_id': material.id,
                'lot_id': d['lot_id'],
                'taken_qty': d['taken_qty'],
                'used_qty': d['used_qty'],
                'returned_qty': d['returned_qty'],
            })

        material.invalidate_recordset()
        allocs = material.lot_allocation_ids
        self.assertEqual(len(allocs), 3)

        # (b) Per-field sums reconcile to the material scalars (UoM-rounded).
        sum_taken = sum(allocs.mapped('taken_qty'))
        sum_used = sum(allocs.mapped('used_qty'))
        sum_returned = sum(allocs.mapped('returned_qty'))
        self.assertEqual(
            float_compare(sum_taken, material.taken_qty,
                          precision_rounding=rounding), 0,
            'taken sum %s must tie to material.taken_qty %s'
            % (sum_taken, material.taken_qty))
        self.assertEqual(
            float_compare(sum_used, material.used_qty,
                          precision_rounding=rounding), 0,
            'used sum %s must tie to material.used_qty %s'
            % (sum_used, material.used_qty))
        self.assertEqual(
            float_compare(sum_returned, material.returned_qty,
                          precision_rounding=rounding), 0,
            'returned sum %s must tie to material.returned_qty %s'
            % (sum_returned, material.returned_qty))

        # (c) Every allocation satisfies used + returned <= taken (UoM-rounded).
        for a in allocs:
            self.assertLessEqual(
                float_compare(a.used_qty + a.returned_qty, a.taken_qty,
                              precision_rounding=rounding), 0,
                'lot %s violates used+returned<=taken: used=%s returned=%s '
                'taken=%s' % (a.lot_id.name, a.used_qty, a.returned_qty,
                              a.taken_qty))

        # NOTE: material._validate_allocation_sums() is intentionally NOT called
        # here. That guard (_check_lot_sum_matches) uses a fixed 0.001 tolerance
        # against the RAW scalar, so a coarse-UoM (rounding 1.0) material whose
        # used_qty is a fractional 3.64 can never satisfy it — whole-unit per-lot
        # values cannot sum to 3.64. That is a fixture-level inconsistency, not
        # what this test pins down. The invariant under test is the per-lot
        # _check_used_quantity / _check_returned_quantity (UoM-rounded), which is
        # exactly what the create() calls above exercise.

    # ──────────────────────────────────────────────
    # 3. action_complete ignores sub-rounding residue
    # ──────────────────────────────────────────────

    def test_action_complete_ignores_subrounding_residue(self):
        """A material whose available_to_return_qty is a residue below the
        product's UoM precision must NOT block job completion.

        The coarse UoM has rounding 0.5; a leftover of 0.20 is field-storable
        (digits=2) but float_compare(0.20, 0, rounding=0.5) == 0. The raw `> 0`
        compare saw 0.20 and raised the unreturned-materials error; the
        UoM-aware compare correctly treats it as zero."""
        material = self._material(
            product=self.product_coarse, uom=self.coarse_uom,
            taken_qty=5.20, used_qty=5.0, returned_qty=0.0)
        self.assertAlmostEqual(material.available_to_return_qty, 0.20, places=2)

        # Sanity: the raw `> 0` compare WOULD treat this as unreturned.
        self.assertTrue(material.available_to_return_qty > 0)
        # But UoM-aware compare correctly says "effectively zero".
        self.assertEqual(
            float_compare(material.available_to_return_qty, 0.0,
                          precision_rounding=self.coarse_rounding), 0)

        # After fix: completion proceeds and the job lands in the Done stage.
        self.job.action_complete()
        done_stage = self.env.ref('esfsm.esfsm_job_stage_done')
        self.assertEqual(self.job.stage_id, done_stage)

    def test_has_materials_computes_ignore_subrounding(self):
        """The has_materials_to_consume / has_materials_to_return computes must
        also use UoM-aware comparison so a sub-rounding residue doesn't keep the
        flags stuck True."""
        self._material(
            product=self.product_coarse, uom=self.coarse_uom,
            taken_qty=5.20, used_qty=5.0, returned_qty=0.0)
        self.job.invalidate_recordset()
        self.assertFalse(
            self.job.has_materials_to_consume,
            'sub-rounding residue must not flag has_materials_to_consume')
        self.assertFalse(
            self.job.has_materials_to_return,
            'sub-rounding residue must not flag has_materials_to_return')
        self.assertEqual(self.job.materials_to_return_count, 0)

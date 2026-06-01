# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Regression tests for the BATCHED picking-lot aggregation in the Phase 3
migration engine.

The classifier used to call ``_get_picking_lot_qtys(material)`` once per
candidate material — a fresh 3-table-JOIN ``cr.execute`` per row (N round
trips). It now collects every candidate ``(job_id, product_id)`` pair and runs
ONE grouped aggregation via ``_batch_picking_lot_qtys``.

These tests pin the equivalence: the batched ``{(job, product): {lot: qty}}``
dict must be byte-for-byte identical to the per-material path for the same
candidate set, across the clean (1 lot), multi-lot (2 lots on one job+product),
gap (no picking history) and untracked cases.

Fully self-contained: every product / lot / location / job is created with a
UNIQUE ``ZZ_MIGB_*`` name so it never collides with the real rows in the
production-clone test database. Done pickings are built through the genuine
confirm/assign/validate flow because ``stock.picking.state`` is a stored
COMPUTED field (raw-SQL forcing is silently overwritten on recompute).
"""

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'esfsm_stock', 'phase3_migration')
class TestPhase3BatchClassification(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.migration = cls.env['esfsm.lot.allocation.migration']
        cls.Material = cls.env['esfsm.job.material']
        cls.src = cls.env.ref('stock.stock_location_stock')
        cls.dest = cls.env.ref('stock.stock_location_customers')
        cls.picking_type_out = cls.env.ref('stock.picking_type_out')

        cls.partner = cls.env['res.partner'].create({'name': 'ZZ_MIGB_PARTNER'})

        # Two distinct tracked products, plus one untracked product.
        cls.prod_a = cls.env['product.product'].create({
            'name': 'ZZ_MIGB_TRACKED_A',
            'type': 'consu', 'is_storable': True,
            'tracking': 'lot', 'standard_price': 50.0,
        })
        cls.prod_b = cls.env['product.product'].create({
            'name': 'ZZ_MIGB_TRACKED_B',
            'type': 'consu', 'is_storable': True,
            'tracking': 'lot', 'standard_price': 70.0,
        })
        cls.prod_untracked = cls.env['product.product'].create({
            'name': 'ZZ_MIGB_UNTRACKED',
            'type': 'consu', 'is_storable': True,
            'tracking': 'none', 'standard_price': 10.0,
        })

        cls.lot_a1 = cls._new_lot(cls, cls.prod_a, 'ZZ_MIGB_A1')
        cls.lot_a2 = cls._new_lot(cls, cls.prod_a, 'ZZ_MIGB_A2')
        cls.lot_b1 = cls._new_lot(cls, cls.prod_b, 'ZZ_MIGB_B1')

        # Three jobs: clean (1 lot), multi-lot (2 lots), gap (no picking).
        cls.job_clean = cls._new_job(cls, 'ZZ_MIGB_JOB_CLEAN')
        cls.job_multi = cls._new_job(cls, 'ZZ_MIGB_JOB_MULTI')
        cls.job_gap = cls._new_job(cls, 'ZZ_MIGB_JOB_GAP')

        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_lot(self, product, name):
        return self.env['stock.lot'].create({
            'name': name, 'product_id': product.id,
        })

    def _new_job(self, name):
        return self.env['esfsm.job'].create({
            'name': name,
            'partner_id': self.partner.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })

    def _material(self, job, product, **kw):
        vals = {
            'job_id': job.id,
            'product_id': product.id,
            'product_uom_id': product.uom_id.id,
            'planned_qty': 10.0,
        }
        vals.update(kw)
        return self.Material.create(vals)

    def _done_picking(self, job, product, lot, qty):
        """Build a genuinely validated (state='done') outgoing picking that
        moves `qty` of `product` for `lot`, stamped with `job` on
        ``esfsm_job_id``. Seeds source stock first so reservation succeeds."""
        self.env['stock.quant']._update_available_quantity(
            product, self.src, qty, lot_id=lot)
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.picking_type_out.id,
            'location_id': self.src.id,
            'location_dest_id': self.dest.id,
            'esfsm_job_id': job.id,
        })
        self.env['stock.move'].create({
            'name': 'ZZ_MIGB_MOVE',
            'product_id': product.id,
            'product_uom_qty': qty,
            'product_uom': product.uom_id.id,
            'picking_id': picking.id,
            'location_id': self.src.id,
            'location_dest_id': self.dest.id,
        })
        picking.action_confirm()
        picking.action_assign()
        for ml in picking.move_line_ids:
            ml.quantity = qty
            if not ml.lot_id:
                ml.lot_id = lot
        picking.button_validate()
        self.assertEqual(picking.state, 'done',
                         'Setup picking must be genuinely validated')
        return picking

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_batch_matches_per_material_for_each_candidate(self):
        """The batched lookup must yield, per candidate (job, product), the
        EXACT same {lot: qty} dict the per-material _get_picking_lot_qtys
        returns. Covers clean (1 lot) and multi-lot (2 lots) candidates."""
        # Clean: one done picking, one lot.
        self._done_picking(self.job_clean, self.prod_a, self.lot_a1, 5.0)
        m_clean = self._material(self.job_clean, self.prod_a, taken_qty=5.0)

        # Multi-lot: same job+product across two done pickings, two lots.
        self._done_picking(self.job_multi, self.prod_a, self.lot_a1, 6.0)
        self._done_picking(self.job_multi, self.prod_a, self.lot_a2, 4.0)
        m_multi = self._material(self.job_multi, self.prod_a, taken_qty=10.0)

        # Second product on the multi job — exercises a distinct pair key.
        self._done_picking(self.job_multi, self.prod_b, self.lot_b1, 3.0)
        m_multi_b = self._material(self.job_multi, self.prod_b, taken_qty=3.0)

        candidates = [m_clean, m_multi, m_multi_b]
        pairs = {(m.job_id.id, m.product_id.id) for m in candidates}
        batched = self.migration._batch_picking_lot_qtys(pairs)

        for m in candidates:
            per_material = self.migration._get_picking_lot_qtys(m)
            key = (m.job_id.id, m.product_id.id)
            self.assertEqual(
                batched.get(key, {}), per_material,
                'Batched result for %s must equal per-material path' % (key,))

        # Spot-check the actual aggregated values.
        self.assertEqual(
            batched[(self.job_clean.id, self.prod_a.id)],
            {self.lot_a1.id: 5.0})
        self.assertEqual(
            batched[(self.job_multi.id, self.prod_a.id)],
            {self.lot_a1.id: 6.0, self.lot_a2.id: 4.0})
        self.assertEqual(
            batched[(self.job_multi.id, self.prod_b.id)],
            {self.lot_b1.id: 3.0})

    def test_batch_empty_pairs_returns_empty_dict(self):
        """Empty candidate set must short-circuit to {} (no `IN ()` SQL error)."""
        self.assertEqual(self.migration._batch_picking_lot_qtys(set()), {})
        self.assertEqual(self.migration._batch_picking_lot_qtys([]), {})

    def test_batch_missing_pair_absent_from_result(self):
        """A (job, product) pair with no done-picking history is absent from
        the batched dict — callers treat that as 'no lot history' (gap),
        identical to _get_picking_lot_qtys returning {}."""
        m_gap = self._material(self.job_gap, self.prod_a, taken_qty=5.0)
        pair = (m_gap.job_id.id, m_gap.product_id.id)
        batched = self.migration._batch_picking_lot_qtys([pair])
        self.assertNotIn(pair, batched)
        self.assertEqual(batched.get(pair, {}), {})
        self.assertEqual(self.migration._get_picking_lot_qtys(m_gap), {})

    def test_classify_buckets_via_batched_path(self):
        """Full _classify_materials run (now batched) must place each material
        in the right bucket with the same lot quantities the per-material path
        would have produced."""
        self._done_picking(self.job_clean, self.prod_a, self.lot_a1, 5.0)
        m_clean = self._material(self.job_clean, self.prod_a, taken_qty=5.0)

        self._done_picking(self.job_multi, self.prod_a, self.lot_a1, 6.0)
        self._done_picking(self.job_multi, self.prod_a, self.lot_a2, 4.0)
        m_multi = self._material(self.job_multi, self.prod_a, taken_qty=10.0)

        m_gap = self._material(self.job_gap, self.prod_a, taken_qty=5.0)

        # Untracked line on the clean job — must be counted, never queried.
        self._material(self.job_clean, self.prod_untracked, taken_qty=7.0)

        stats = self.migration._classify_materials()

        clean_entry = next(
            (e for e in stats['clean'] if e['material_id'] == m_clean.id), None)
        self.assertIsNotNone(clean_entry, 'clean material must be in clean bucket')
        self.assertEqual(clean_entry['lot_id'], self.lot_a1.id)
        self.assertEqual(clean_entry['qty'], 5.0)

        multi_entry = next(
            (e for e in stats['multi_lot'] if e['material_id'] == m_multi.id), None)
        self.assertIsNotNone(multi_entry, 'multi material must be in multi_lot bucket')
        self.assertEqual(
            dict(multi_entry['lots']),
            {self.lot_a1.id: 6.0, self.lot_a2.id: 4.0})

        self.assertIn(m_gap.id, stats['gap'])
        self.assertGreaterEqual(stats['untracked'], 1)

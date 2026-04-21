# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install', 'esfsm_stock', 'phase3_migration')
class TestPhase3Migration(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'MigTest Customer'})
        cls.product = cls.env['product.product'].create({
            'name': 'MigTest Tracked',
            'type': 'consu', 'is_storable': True,
            'tracking': 'lot', 'standard_price': 50.0,
        })
        cls.lot_a = cls.env['stock.lot'].create({'name': 'MIG-A', 'product_id': cls.product.id})
        cls.lot_b = cls.env['stock.lot'].create({'name': 'MIG-B', 'product_id': cls.product.id})
        cls.job = cls.env['esfsm.job'].create({
            'name': 'MIG-TEST-JOB',
            'partner_id': cls.partner.id,
            'scheduled_date_start': '2025-01-15 10:00:00',
        })
        cls.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

    def _material(self, **kw):
        vals = {
            'job_id': self.job.id,
            'product_id': self.product.id,
            'product_uom_id': self.product.uom_id.id,
            'planned_qty': 10.0,
        }
        vals.update(kw)
        return self.env['esfsm.job.material'].create(vals)

    def test_01_dry_run_returns_stats(self):
        migration = self.env['esfsm.lot.allocation.migration']
        result = migration.dry_run()
        self.assertIn('stats', result)
        self.assertIn('report', result)
        self.assertIn('DRY RUN', result['report'])
        self.assertIsInstance(result['stats']['tracked_total'], int)

    def test_02_gap_classification(self):
        """Material with taken_qty > 0 but no picking history → gap bucket."""
        m = self._material(taken_qty=5.0)
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        self.assertIn(m.id, stats['gap'])

    def test_03_untracked_skipped(self):
        """tracking=none products → 'untracked' count, not in buckets."""
        untracked = self.env['product.product'].create({
            'name': 'MigTest Untracked', 'type': 'consu',
            'is_storable': True, 'standard_price': 10.0,
        })
        self.env['esfsm.job.material'].create({
            'job_id': self.job.id, 'product_id': untracked.id,
            'product_uom_id': untracked.uom_id.id,
            'planned_qty': 5.0, 'taken_qty': 5.0,
        })
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        self.assertGreaterEqual(stats['untracked'], 1)

    def test_04_already_migrated_counted(self):
        """Material with existing allocations → 'already_migrated' count."""
        m = self._material(taken_qty=5.0)
        self.env['esfsm.job.material.lot'].create({
            'material_id': m.id, 'lot_id': self.lot_a.id, 'taken_qty': 5.0,
        })
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        self.assertGreaterEqual(stats['already_migrated'], 1)

    def test_05_ambiguous_detection(self):
        """Same job+product on 2 materials → ambiguous bucket."""
        m1 = self._material(taken_qty=3.0)
        m2 = self._material(taken_qty=4.0)
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        ambig_combos = stats['ambiguous']
        combo_match = [
            c for c in ambig_combos
            if c['job_id'] == self.job.id and c['product_id'] == self.product.id
        ]
        self.assertEqual(len(combo_match), 1)
        self.assertIn(m1.id, combo_match[0]['material_ids'])
        self.assertIn(m2.id, combo_match[0]['material_ids'])

    def test_06_migrate_gap_flags(self):
        """Commit migration: gap materials get lot_allocation_historical_gap=True."""
        m = self._material(taken_qty=5.0)
        migration = self.env['esfsm.lot.allocation.migration']
        result = migration.migrate(commit=True)
        self.assertTrue(result['committed'])
        m.invalidate_recordset(['lot_allocation_historical_gap', 'lot_id_legacy_archive'])
        self.assertTrue(m.lot_allocation_historical_gap)
        self.assertTrue(m.lot_id_legacy_archive)

    def test_07_migrate_rejects_when_flag_off(self):
        """Migration requires feature flag ON."""
        self.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False')
        with self.assertRaises(UserError):
            self.env['esfsm.lot.allocation.migration'].migrate(commit=True)
        # restore
        self.env['ir.config_parameter'].sudo().set_param(
            'esfsm_stock.per_lot_allocations_enabled', 'True')

    def test_08a_resolution_wizard_default_get_when_no_combos(self):
        """Regression: default_get must initialize all scalar fields declared
        in the view when no combo is pending. material_ids is no longer in
        the view (removed to avoid legacy BasicModel _abandonRecords crash
        on invisible Many2many with half-initialized state)."""
        wizard_model = self.env['esfsm.lot.resolution.wizard']

        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        for combo in stats['ambiguous']:
            materials = self.env['esfsm.job.material'].browse(combo['material_ids'])
            materials.with_context(
                skip_allocation_sum_check=True,
            ).write({'lot_allocation_historical_gap': True})

        self.assertIsNone(wizard_model._find_next_ambiguous())

        defaults = wizard_model.default_get([
            'job_id', 'product_id', 'line_ids',
            'total_material_taken', 'total_lot_qty', 'remaining_combos',
        ])
        for field_name in ('line_ids', 'job_id', 'product_id',
                           'total_material_taken', 'total_lot_qty',
                           'remaining_combos'):
            self.assertIn(
                field_name, defaults,
                msg=f'default_get missing {field_name} when no combo'
            )

    def test_08b_settings_resolve_button_skips_empty_wizard(self):
        """Regression: Settings button must not open empty wizard."""
        # Resolve any test-created ambiguous combos first
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        for combo in stats['ambiguous']:
            materials = self.env['esfsm.job.material'].browse(combo['material_ids'])
            materials.with_context(
                skip_allocation_sum_check=True,
            ).write({'lot_allocation_historical_gap': True})

        settings = self.env['res.config.settings'].create({})
        result = settings.action_phase3_resolve_ambiguous()
        # Should return a notification, not an act_window
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')

    def test_08_rollback_restores_lot_id(self):
        """Rollback restores lot_id and deletes allocations."""
        m = self._material(taken_qty=5.0, lot_id=self.lot_a.id)
        migration = self.env['esfsm.lot.allocation.migration']
        migration._snapshot_legacy(m)
        # Simulate post-migration state: lot_id cleared, allocation exists
        m.with_context(skip_allocation_sum_check=True).write({'lot_id': False})
        self.env['esfsm.job.material.lot'].with_context(
            skip_allocation_sum_check=True
        ).create({
            'material_id': m.id, 'lot_id': self.lot_a.id, 'taken_qty': 5.0,
        })
        result = migration.rollback()
        self.assertGreaterEqual(result['restored'], 1)
        m.invalidate_recordset()
        self.assertEqual(m.lot_id, self.lot_a)
        self.assertFalse(m.lot_allocation_ids)
        self.assertFalse(m.lot_id_legacy_archive)

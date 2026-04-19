# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, _
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    esfsm_per_lot_allocations_enabled = fields.Boolean(
        string='Вклучи алокации по лот (Phase 2+)',
        config_parameter='esfsm_stock.per_lot_allocations_enabled',
        help='Кога е вклучено, Take/Consume/Return wizards пишуваат во новиот '
             'esfsm.job.material.lot модел. Оставете OFF во Phase 1 (само schema deploy).',
    )

    def action_phase3_dry_run(self):
        """Trigger Phase 3 migration dry-run — classifies materials, produces report."""
        self.ensure_one()
        result = self.env['esfsm.lot.allocation.migration'].dry_run()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Phase 3 Dry Run'),
                'message': result['report'],
                'type': 'info',
                'sticky': True,
            },
        }

    def action_phase3_migrate_commit(self):
        """Execute Phase 3 migration — writes allocations + gap flags."""
        self.ensure_one()
        if not self.env['esfsm.job.material']._is_per_lot_enabled():
            raise UserError(_(
                'Прво вклучете го feature flag-от (Алокации по лот).'
            ))
        result = self.env['esfsm.lot.allocation.migration'].migrate(commit=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Phase 3 Migration COMMITTED'),
                'message': result['report'],
                'type': 'success',
                'sticky': True,
            },
        }

    def action_phase3_resolve_ambiguous(self):
        """Open resolution wizard for ambiguous combos."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.lot.resolution.wizard',
            'view_mode': 'form',
            'target': 'new',
            'name': _('Resolve Ambiguous Lot Allocations'),
        }

    def action_phase3_rollback(self):
        """Restore lot_id from JSON archive; delete all allocations. Destructive."""
        self.ensure_one()
        result = self.env['esfsm.lot.allocation.migration'].rollback()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Phase 3 Rollback COMPLETE'),
                'message': _('Restored %s materials.') % result['restored'],
                'type': 'warning',
                'sticky': True,
            },
        }

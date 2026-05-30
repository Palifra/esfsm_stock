# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class StockMove(models.Model):
    _inherit = 'stock.move'

    esfsm_material_line_id = fields.Many2one(
        'esfsm.job.material', string='ESFSM Material Line',
        index=True, ondelete='set null',
        help='The ESFSM job material line this move was created for (used to '
             'attribute lot allocations to the correct material when one '
             'picking serves several material lines of the same product).')

    def _prepare_merge_moves_distinct_fields(self):
        # Moves created for DIFFERENT esfsm material lines must never be merged,
        # otherwise per-material lot-allocation attribution (see
        # esfsm.job.material._sync_allocation_on_take) breaks: a merged move
        # would feed both materials' syncs from one move-line set.
        distinct_fields = super()._prepare_merge_moves_distinct_fields()
        distinct_fields.append('esfsm_material_line_id')
        return distinct_fields

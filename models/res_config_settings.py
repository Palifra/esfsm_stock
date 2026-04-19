# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    esfsm_per_lot_allocations_enabled = fields.Boolean(
        string='Вклучи алокации по лот (Phase 2+)',
        config_parameter='esfsm_stock.per_lot_allocations_enabled',
        help='Кога е вклучено, Take/Consume/Return wizards пишуваат во новиот '
             'esfsm.job.material.lot модел. Оставете OFF во Phase 1 (само schema deploy).',
    )

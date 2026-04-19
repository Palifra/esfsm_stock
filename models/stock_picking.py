# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    esfsm_job_id = fields.Many2one(
        'esfsm.job',
        string='FSM Работа',
        help='Работа за теренска услуга поврзана со овој picking'
    )
    esfsm_allocation_synced = fields.Boolean(
        string='Lot allocation synced',
        default=False,
        copy=False,
        help='True after _sync_allocation_on_take has mirrored this picking into '
             'esfsm.job.material.lot. Prevents double-sync on retry.',
    )

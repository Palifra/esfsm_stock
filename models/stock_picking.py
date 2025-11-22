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

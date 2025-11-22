# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    vehicle_id = fields.Many2one(
        'fleet.vehicle',
        string='Лично возило',
        help='Возило доделено на овој вработен за теренска работа'
    )

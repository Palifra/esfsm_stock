# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class EsfsmTeam(models.Model):
    _inherit = 'esfsm.team'

    vehicle_id = fields.Many2one(
        'fleet.vehicle',
        string='Возило на тим',
        help='Возило доделено на овој тим за транспорт на материјали'
    )
    stock_location_id = fields.Many2one(
        'stock.location',
        string='Локација на залихи',
        related='vehicle_id.stock_location_id',
        readonly=True,
        store=False,
        help='Локација на залихи од возилото на тимот'
    )

# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class EsfsmJobMaterialLot(models.Model):
    _name = 'esfsm.job.material.lot'
    _description = 'Алокација на лот за материјал'
    _order = 'material_id, sequence, id'

    material_id = fields.Many2one(
        'esfsm.job.material',
        string='Материјал',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот/Сериски број',
        required=True,
        domain="[('product_id', '=', product_id)]",
        ondelete='restrict',
    )

    taken_qty = fields.Float(
        string='Земено',
        digits='Product Unit of Measure',
        default=0.0,
    )
    used_qty = fields.Float(
        string='Искористено',
        digits='Product Unit of Measure',
        default=0.0,
    )
    returned_qty = fields.Float(
        string='Вратено',
        digits='Product Unit of Measure',
        default=0.0,
    )

    available_to_consume_qty = fields.Float(
        string='Достапно за трошење',
        compute='_compute_available_qtys',
        store=True,
        digits='Product Unit of Measure',
    )
    available_to_return_qty = fields.Float(
        string='Достапно за враќање',
        compute='_compute_available_qtys',
        store=True,
        digits='Product Unit of Measure',
    )

    product_id = fields.Many2one(
        related='material_id.product_id',
        store=True,
        readonly=True,
    )
    product_uom_id = fields.Many2one(
        related='material_id.product_uom_id',
        readonly=True,
    )
    job_id = fields.Many2one(
        related='material_id.job_id',
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related='material_id.company_id',
        store=True,
        readonly=True,
    )

    _sql_constraints = [
        ('check_positive_taken', 'CHECK(taken_qty >= 0)',
         'Земената количина по лот не може да биде негативна.'),
        ('check_positive_used', 'CHECK(used_qty >= 0)',
         'Искористената количина по лот не може да биде негативна.'),
        ('check_positive_returned', 'CHECK(returned_qty >= 0)',
         'Вратената количина по лот не може да биде негативна.'),
        ('unique_material_lot', 'UNIQUE(material_id, lot_id)',
         'Секој лот може да се појави само еднаш по материјал.'),
    ]

    @api.depends('taken_qty', 'used_qty', 'returned_qty')
    def _compute_available_qtys(self):
        for line in self:
            remaining = line.taken_qty - line.used_qty - line.returned_qty
            line.available_to_consume_qty = remaining
            line.available_to_return_qty = remaining

    @api.constrains('used_qty', 'taken_qty')
    def _check_used_quantity(self):
        for line in self:
            if line.used_qty > line.taken_qty:
                raise ValidationError(_(
                    'Искористено (%(used)s) > земено (%(taken)s) за лот %(lot)s',
                    used=line.used_qty,
                    taken=line.taken_qty,
                    lot=line.lot_id.name,
                ))

    @api.constrains('returned_qty', 'taken_qty', 'used_qty')
    def _check_returned_quantity(self):
        for line in self:
            available = line.taken_qty - line.used_qty
            if line.returned_qty > available:
                raise ValidationError(_(
                    'Вратено (%(ret)s) > достапно (%(avail)s) за лот %(lot)s',
                    ret=line.returned_qty,
                    avail=available,
                    lot=line.lot_id.name,
                ))

# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmReturnMaterialWizard(models.TransientModel):
    _name = 'esfsm.return.material.wizard'
    _description = 'Wizard за враќање материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
        help='Работа од која се враќаат материјали'
    )
    line_ids = fields.One2many(
        'esfsm.return.material.wizard.line',
        'wizard_id',
        string='Линии',
        help='Материјали за враќање'
    )

    @api.model
    def default_get(self, fields_list):
        """Pre-populate wizard with returnable materials"""
        res = super().default_get(fields_list)

        # Get job from context
        job_id = self.env.context.get('active_id')
        if not job_id:
            return res

        job = self.env['esfsm.job'].browse(job_id)
        res['job_id'] = job_id

        # Find materials with quantity available to return
        returnable_materials = job.material_ids.filtered(
            lambda m: m.available_to_return_qty > 0
        )

        # Create wizard lines
        lines = []
        for material in returnable_materials:
            lines.append((0, 0, {
                'material_line_id': material.id,
                'product_id': material.product_id.id,
                'product_uom_id': material.product_uom_id.id,
                'lot_id': material.lot_id.id if material.lot_id else False,
                'available_qty': material.available_to_return_qty,
                'return_qty': material.available_to_return_qty,  # Default to full return
            }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create return picking using Враќање на Реверс type"""
        self.ensure_one()

        lines_to_return = self.line_ids.filtered(lambda l: l.return_qty > 0)
        if not lines_to_return:
            raise ValidationError(_('Нема материјали за враќање.'))

        job = self.job_id

        # Use StockPickingService to create picking
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_return_picking(job, lines_to_return)

        # Update material lines returned_qty (bypass write() warning)
        for line in lines_to_return:
            new_returned = line.material_line_id.returned_qty + line.return_qty
            line.material_line_id.with_context(skip_auto_picking=True).write({
                'returned_qty': new_returned
            })

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EsfsmReturnMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.return.material.wizard.line'
    _description = 'Линија за враќање материјал'

    wizard_id = fields.Many2one(
        'esfsm.return.material.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade'
    )
    material_line_id = fields.Many2one(
        'esfsm.job.material',
        string='Материјална линија',
        required=True,
        readonly=True
    )
    product_id = fields.Many2one(
        'product.product',
        string='Производ',
        required=True,
        readonly=True
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Мерна единица',
        required=True,
        readonly=True
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот/Сериски број',
        readonly=True,
        help='Лот или сериски број за производи со следење'
    )
    available_qty = fields.Float(
        string='Достапно',
        readonly=True,
        digits='Product Unit of Measure'
    )
    return_qty = fields.Float(
        string='Количина за враќање',
        digits='Product Unit of Measure',
        required=True
    )

    @api.constrains('return_qty', 'available_qty')
    def _check_return_qty(self):
        """Validate return quantity"""
        for line in self:
            if line.return_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.return_qty > line.available_qty:
                raise ValidationError(_(
                    'Количината за враќање (%s) не може да биде поголема од достапната (%s) за %s'
                ) % (line.return_qty, line.available_qty, line.product_id.name))

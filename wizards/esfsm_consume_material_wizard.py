# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmConsumeMaterialWizard(models.TransientModel):
    _name = 'esfsm.consume.material.wizard'
    _description = 'Wizard за потрошувачка на материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        'esfsm.consume.material.wizard.line',
        'wizard_id',
        string='Линии',
    )

    @api.model
    def default_get(self, fields_list):
        """Pre-populate wizard with consumable materials"""
        res = super().default_get(fields_list)

        job_id = self.env.context.get('active_id')
        if not job_id:
            return res

        job = self.env['esfsm.job'].browse(job_id)
        res['job_id'] = job_id

        # Find materials that can be consumed (taken > used + returned)
        lines = []
        for material in job.material_ids:
            available_to_consume = material.taken_qty - material.used_qty - material.returned_qty
            if available_to_consume > 0:
                lines.append((0, 0, {
                    'material_line_id': material.id,
                    'product_id': material.product_id.id,
                    'product_uom_id': material.product_uom_id.id,
                    'lot_id': material.lot_id.id if material.lot_id else False,
                    'taken_qty': material.taken_qty,
                    'already_used_qty': material.used_qty,
                    'already_returned_qty': material.returned_qty,
                    'available_to_consume': available_to_consume,
                    'consume_qty': available_to_consume,  # Default: consume all available
                }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create Испратница picking for consumed materials"""
        self.ensure_one()

        lines_to_consume = self.line_ids.filtered(lambda l: l.consume_qty > 0)
        if not lines_to_consume:
            raise ValidationError(_('Нема материјали за потрошувачка.'))

        job = self.job_id

        # Use StockPickingService to create picking
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_delivery_picking(job, lines_to_consume)

        # Update material lines used_qty (bypass write() auto-picking)
        for line in lines_to_consume:
            new_used = line.material_line_id.used_qty + line.consume_qty
            line.material_line_id.with_context(skip_auto_picking=True).write({
                'used_qty': new_used
            })

        # Check if there are materials to return
        remaining = sum(
            m.taken_qty - m.used_qty - m.returned_qty
            for m in job.material_ids
        )

        if remaining > 0:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Успешно'),
                    'message': _('Потрошено %d материјали. Остануваат %.2f за враќање.') % (
                        len(lines_to_consume), remaining
                    ),
                    'type': 'warning',
                    'sticky': False,
                    'next': {
                        'type': 'ir.actions.act_window',
                        'res_model': 'stock.picking',
                        'res_id': picking.id,
                        'view_mode': 'form',
                        'target': 'current',
                    }
                }
            }

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }



class EsfsmConsumeMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.consume.material.wizard.line'
    _description = 'Линија за потрошувачка на материјал'

    wizard_id = fields.Many2one(
        'esfsm.consume.material.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade',
    )
    material_line_id = fields.Many2one(
        'esfsm.job.material',
        string='Материјална линија',
        required=True,
        readonly=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Производ',
        required=True,
        readonly=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Мерна единица',
        required=True,
        readonly=True,
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот',
        readonly=True,
    )
    taken_qty = fields.Float(
        string='Земено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    already_used_qty = fields.Float(
        string='Веќе потрошено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    already_returned_qty = fields.Float(
        string='Веќе вратено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    available_to_consume = fields.Float(
        string='Достапно',
        readonly=True,
        digits='Product Unit of Measure',
    )
    consume_qty = fields.Float(
        string='Потроши',
        digits='Product Unit of Measure',
    )

    @api.constrains('consume_qty', 'available_to_consume')
    def _check_consume_qty(self):
        """Validate consume quantity"""
        for line in self:
            if line.consume_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.consume_qty > line.available_to_consume:
                raise ValidationError(_(
                    'Количината за потрошувачка (%s) не може да биде поголема од достапната (%s) за %s'
                ) % (line.consume_qty, line.available_to_consume, line.product_id.name))

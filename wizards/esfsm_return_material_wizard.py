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
                'available_qty': material.available_to_return_qty,
                'return_qty': material.available_to_return_qty,  # Default to full return
            }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create return picking using Враќање на Реверс type"""
        self.ensure_one()

        if not self.line_ids:
            raise ValidationError(_('Нема материјали за враќање.'))

        job = self.job_id

        # Get responsible technician name (or first assigned, or 'Unknown')
        technician_name = (
            job.material_responsible_id.name if job.material_responsible_id
            else job.employee_ids[0].name if job.employee_ids
            else 'Непознат'
        )

        # Find "Враќање на Реверс" picking type (materials returned from employee)
        picking_type = self.env['stock.picking.type'].search([
            ('name', '=', 'Враќање на Реверс'),
            ('company_id', '=', job.company_id.id)
        ], limit=1)

        # Fallback to generic internal if not found
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', job.company_id.id)
            ], limit=1)

        # Get source (technician) and destination (warehouse) locations
        source_location = job._get_source_location()  # Technician/vehicle location
        dest_location = picking_type.default_location_dest_id or self.env.ref('stock.stock_location_stock')

        # Create picking with Враќање на Реверс type and technician name
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'origin': f"{job.name} - Повратница - {technician_name}",
        })

        # Create stock moves for each line
        for line in self.line_ids:
            if line.return_qty <= 0:
                continue

            self.env['stock.move'].create({
                'name': f"{job.name} - {line.product_id.name}",
                'product_id': line.product_id.id,
                'product_uom_qty': line.return_qty,
                'product_uom': line.product_uom_id.id,
                'picking_id': picking.id,
                'location_id': source_location.id,
                'location_dest_id': dest_location.id,
            })

            # Update material line returned_qty
            line.material_line_id.returned_qty += line.return_qty

        # Post message to job chatter
        job.message_post(
            body=_('Повратница од %s: %d материјали - %s') % (technician_name, len(self.line_ids), picking.name)
        )

        # Return action to view created picking
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

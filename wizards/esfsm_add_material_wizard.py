# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmAddMaterialWizard(models.TransientModel):
    _name = 'esfsm.add.material.wizard'
    _description = 'Wizard за додавање материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
        help='Работа на која и се додаваат материјали'
    )
    line_ids = fields.One2many(
        'esfsm.add.material.wizard.line',
        'wizard_id',
        string='Линии',
        help='Материјали за додавање'
    )

    @api.model
    def default_get(self, fields_list):
        """Pre-fill job_id from context"""
        res = super().default_get(fields_list)

        job_id = self.env.context.get('active_id')
        if job_id:
            res['job_id'] = job_id

        return res

    def action_confirm(self):
        """Create material lines and supply picking using Реверс type"""
        self.ensure_one()

        if not self.line_ids:
            raise ValidationError(_('Нема материјали за додавање.'))

        job = self.job_id

        # Get first technician name for the document
        technician_name = job.employee_ids[0].name if job.employee_ids else 'Непознат'

        # Find "Реверс" picking type (materials issued to employee)
        picking_type = self.env['stock.picking.type'].search([
            ('name', '=', 'Реверс'),
            ('company_id', '=', job.company_id.id)
        ], limit=1)

        # Fallback to generic internal if Реверс not found
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', job.company_id.id)
            ], limit=1)

        # Get source from picking type defaults, destination from job
        source_location = picking_type.default_location_src_id or self.env.ref('stock.stock_location_stock')
        dest_location = job._get_source_location()

        # Create picking with Реверс type and technician name
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'origin': f"{job.name} - Реверс - {technician_name}",
        })

        # Create material lines and stock moves
        for line in self.line_ids:
            if line.qty <= 0:
                continue

            # Create or update material line on job
            existing_material = job.material_ids.filtered(
                lambda m: m.product_id == line.product_id
            )

            if existing_material:
                # Update existing line - add to taken_qty
                existing_material[0].taken_qty += line.qty
            else:
                # Create new material line
                self.env['esfsm.job.material'].create({
                    'job_id': job.id,
                    'product_id': line.product_id.id,
                    'product_uom_id': line.product_uom_id.id,
                    'price_unit': line.product_id.standard_price,
                    'planned_qty': line.qty,
                    'taken_qty': line.qty,
                })

            # Create stock move
            self.env['stock.move'].create({
                'name': f"{job.name} - {line.product_id.name}",
                'product_id': line.product_id.id,
                'product_uom_qty': line.qty,
                'product_uom': line.product_uom_id.id,
                'picking_id': picking.id,
                'location_id': source_location.id,
                'location_dest_id': dest_location.id,
            })

        # Post message to job chatter with technician name
        technician_name = job.employee_ids[0].name if job.employee_ids else 'Непознат'
        job.message_post(
            body=_('Реверс издаден на %s: %d материјали - %s') % (technician_name, len(self.line_ids), picking.name)
        )

        # Return action to view created picking
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EsfsmAddMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.add.material.wizard.line'
    _description = 'Линија за додавање материјал'

    wizard_id = fields.Many2one(
        'esfsm.add.material.wizard',
        string='Wizard',
        required=True,
        ondelete='cascade'
    )
    product_id = fields.Many2one(
        'product.product',
        string='Производ',
        required=True,
        domain=[('type', 'in', ['consu', 'product'])]
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Мерна единица',
        required=True
    )
    qty = fields.Float(
        string='Количина',
        digits='Product Unit of Measure',
        required=True,
        default=1.0
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Auto-fill UoM from product"""
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id

    @api.constrains('qty')
    def _check_qty(self):
        """Validate quantity is positive"""
        for line in self:
            if line.qty <= 0:
                raise ValidationError(_('Количината мора да биде поголема од 0.'))

# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmTakeMaterialWizard(models.TransientModel):
    _name = 'esfsm.take.material.wizard'
    _description = 'Wizard за превземање материјали'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        'esfsm.take.material.wizard.line',
        'wizard_id',
        string='Линии',
    )
    source_location_id = fields.Many2one(
        'stock.location',
        string='Извор (магацин)',
        compute='_compute_locations',
    )
    dest_location_id = fields.Many2one(
        'stock.location',
        string='Дестинација (техничар)',
        compute='_compute_locations',
    )

    @api.depends('job_id')
    def _compute_locations(self):
        """Compute source and destination locations"""
        for wizard in self:
            if wizard.job_id:
                # Source: main warehouse
                warehouse = self.env['stock.warehouse'].search([
                    ('company_id', '=', wizard.job_id.company_id.id)
                ], limit=1)
                wizard.source_location_id = warehouse.lot_stock_id if warehouse else False
                # Destination: technician/vehicle location
                wizard.dest_location_id = wizard.job_id._get_source_location()
            else:
                wizard.source_location_id = False
                wizard.dest_location_id = False

    @api.model
    def default_get(self, fields_list):
        """Pre-populate wizard with materials to take"""
        res = super().default_get(fields_list)

        job_id = self.env.context.get('active_id')
        if not job_id:
            return res

        job = self.env['esfsm.job'].browse(job_id)
        res['job_id'] = job_id

        # Get source location for stock check
        warehouse = self.env['stock.warehouse'].search([
            ('company_id', '=', job.company_id.id)
        ], limit=1)
        source_location = warehouse.lot_stock_id if warehouse else False

        # Find materials with planned_qty > taken_qty
        materials_to_take = job.material_ids.filtered(
            lambda m: m.planned_qty > m.taken_qty
        )

        lines = []
        for material in materials_to_take:
            qty_to_take = material.planned_qty - material.taken_qty

            # Check available stock
            available_qty = 0.0
            if source_location:
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', material.product_id.id),
                    ('location_id', '=', source_location.id),
                ])
                available_qty = sum(quants.mapped('quantity'))

            # Determine status
            if available_qty <= 0:
                status = 'no_stock'
                suggested_qty = 0.0
            elif available_qty < qty_to_take:
                status = 'partial'
                suggested_qty = available_qty
            else:
                status = 'ok'
                suggested_qty = qty_to_take

            lines.append((0, 0, {
                'material_line_id': material.id,
                'product_id': material.product_id.id,
                'product_uom_id': material.product_uom_id.id,
                'lot_id': material.lot_id.id if material.lot_id else False,
                'planned_qty': material.planned_qty,
                'already_taken_qty': material.taken_qty,
                'qty_to_take': qty_to_take,
                'available_qty': available_qty,
                'take_qty': suggested_qty,
                'status': status,
            }))

        res['line_ids'] = lines
        return res

    def action_confirm(self):
        """Create Реверс picking for taken materials"""
        self.ensure_one()

        lines_to_take = self.line_ids.filtered(lambda l: l.take_qty > 0)
        if not lines_to_take:
            raise ValidationError(_('Нема материјали за превземање.'))

        job = self.job_id

        # Get technician name
        technician_name = (
            job.material_responsible_id.name if job.material_responsible_id
            else job.employee_ids[0].name if job.employee_ids
            else 'Непознат'
        )

        # Find "Реверс" picking type
        picking_type = self.env['stock.picking.type'].search([
            ('name', '=', 'Реверс'),
            ('company_id', '=', job.company_id.id)
        ], limit=1)

        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('code', '=', 'internal'),
                ('company_id', '=', job.company_id.id)
            ], limit=1)

        # Get locations
        source_location = self.source_location_id or self.env.ref('stock.stock_location_stock')
        dest_location = self.dest_location_id or job._get_source_location()

        # Create picking
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'origin': f"{job.name} - Реверс - {technician_name}",
        })

        # Create moves for each line
        for line in lines_to_take:
            move = self.env['stock.move'].create({
                'name': f"{job.name} - {line.product_id.name}",
                'product_id': line.product_id.id,
                'product_uom_qty': line.take_qty,
                'product_uom': line.product_uom_id.id,
                'picking_id': picking.id,
                'location_id': source_location.id,
                'location_dest_id': dest_location.id,
            })

            # Handle lot tracking
            if line.lot_id:
                self.env['stock.move.line'].create({
                    'move_id': move.id,
                    'product_id': line.product_id.id,
                    'product_uom_id': line.product_uom_id.id,
                    'location_id': source_location.id,
                    'location_dest_id': dest_location.id,
                    'lot_id': line.lot_id.id,
                    'quantity': line.take_qty,
                    'picking_id': picking.id,
                })

        # Validate picking
        picking.action_confirm()
        picking.action_assign()

        # Set quantities done
        for move in picking.move_ids:
            if not move.move_line_ids:
                move.quantity = move.product_uom_qty
            else:
                for ml in move.move_line_ids:
                    ml.quantity = ml.quantity or move.product_uom_qty

        picking.button_validate()

        # Update material lines taken_qty (bypass write() auto-picking)
        for line in lines_to_take:
            new_taken = line.material_line_id.taken_qty + line.take_qty
            line.material_line_id.with_context(skip_auto_picking=True).write({
                'taken_qty': new_taken
            })

        # Post message
        material_list = ', '.join([
            f"{l.product_id.name} ({l.take_qty} {l.product_uom_id.name})"
            for l in lines_to_take
        ])
        job.message_post(
            body=_('Реверс издаден на %s: %s - %s') % (
                technician_name, material_list, picking.name
            )
        )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EsfsmTakeMaterialWizardLine(models.TransientModel):
    _name = 'esfsm.take.material.wizard.line'
    _description = 'Линија за превземање материјал'

    wizard_id = fields.Many2one(
        'esfsm.take.material.wizard',
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
    planned_qty = fields.Float(
        string='Планирано',
        readonly=True,
        digits='Product Unit of Measure',
    )
    already_taken_qty = fields.Float(
        string='Веќе земено',
        readonly=True,
        digits='Product Unit of Measure',
    )
    qty_to_take = fields.Float(
        string='За превземање',
        readonly=True,
        digits='Product Unit of Measure',
    )
    available_qty = fields.Float(
        string='На залиха',
        readonly=True,
        digits='Product Unit of Measure',
    )
    take_qty = fields.Float(
        string='Превземи',
        digits='Product Unit of Measure',
    )
    status = fields.Selection([
        ('ok', 'Достапно'),
        ('partial', 'Делумно'),
        ('no_stock', 'Нема залиха'),
    ], string='Статус', readonly=True)

    @api.constrains('take_qty', 'available_qty', 'qty_to_take')
    def _check_take_qty(self):
        """Validate take quantity"""
        for line in self:
            if line.take_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.take_qty > line.available_qty:
                raise ValidationError(_(
                    'Количината за превземање (%s) не може да биде поголема од залихата (%s) за %s'
                ) % (line.take_qty, line.available_qty, line.product_id.name))

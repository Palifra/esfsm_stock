# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

import logging
from markupsafe import Markup
from odoo import api, models, fields, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


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
        _logger.info("=== TAKE WIZARD default_get ===")
        _logger.info(f"Job: {job.name}, Materials to take: {len(materials_to_take)}")
        _logger.info(f"Source location: {source_location.complete_name if source_location else 'NONE'}")

        for material in materials_to_take:
            qty_to_take = material.planned_qty - material.taken_qty

            # Check available stock (include child locations)
            available_qty = 0.0
            if source_location:
                # Get all child locations too
                child_locations = self.env['stock.location'].search([
                    ('id', 'child_of', source_location.id),
                    ('usage', '=', 'internal'),
                ])
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', material.product_id.id),
                    ('location_id', 'in', child_locations.ids),
                    ('quantity', '>', 0),
                ])
                available_qty = sum(quants.mapped('quantity'))
                _logger.info(f"  Product: {material.product_id.name[:30]}, Need: {qty_to_take}, Stock: {available_qty}")

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

        job = self.job_id

        _logger.info("=== ACTION_CONFIRM ===")
        _logger.info(f"Wizard ID: {self.id}, Job: {job.name}")
        _logger.info(f"Total lines: {len(self.line_ids)}")
        for line in self.line_ids:
            _logger.info(f"  Line: product={line.product_id.name[:30] if line.product_id else 'NONE'}, take_qty={line.take_qty}, status={line.status}")

        # Separate lines by status
        lines_to_take = self.line_ids.filtered(lambda l: l.take_qty > 0 and l.material_line_id and l.product_id)
        partial_lines = self.line_ids.filtered(lambda l: l.status == 'partial')
        no_stock_lines = self.line_ids.filtered(lambda l: l.status == 'no_stock')

        # Build notification messages
        messages = []

        # No stock warning
        if no_stock_lines:
            no_stock_items = []
            for line in no_stock_lines:
                if line.product_id:
                    no_stock_items.append(f"• {line.product_id.name}: потребно {line.qty_to_take} {line.product_uom_id.name if line.product_uom_id else ''}")
            if no_stock_items:
                messages.append(_("<b>⚠️ Нема на залиха:</b><br/>") + "<br/>".join(no_stock_items))

        # Partial stock warning
        if partial_lines:
            partial_items = []
            for line in partial_lines:
                if line.product_id:
                    missing = line.qty_to_take - line.available_qty
                    partial_items.append(
                        f"• {line.product_id.name}: земено {line.take_qty}, недостасува {missing:.2f} {line.product_uom_id.name if line.product_uom_id else ''}"
                    )
            if partial_items:
                messages.append(_("<b>⚠️ Делумно превземено:</b><br/>") + "<br/>".join(partial_items))

        # Check if we have anything to take
        if not lines_to_take:
            # Build detailed error message
            error_parts = [_('Нема материјали на залиха за превземање.')]

            if no_stock_lines:
                error_parts.append(_('\n\n⚠️ Нема на залиха:'))
                for line in no_stock_lines:
                    if line.product_id:
                        error_parts.append(f"  • {line.product_id.name}: потребно {line.qty_to_take}")

            if partial_lines:
                error_parts.append(_('\n\n⚠️ Делумно достапно:'))
                for line in partial_lines:
                    if line.product_id:
                        error_parts.append(f"  • {line.product_id.name}: достапно {line.available_qty} од {line.qty_to_take}")

            # Also post to chatter for record
            if messages:
                job.message_post(
                    body=Markup("<br/><br/>".join(messages)),
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )

            raise ValidationError('\n'.join(error_parts))

        # Use StockPickingService to create picking
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_reverse_picking(job, lines_to_take)

        # Update material lines taken_qty (bypass write() auto-picking)
        for line in lines_to_take:
            new_taken = line.material_line_id.taken_qty + line.take_qty
            line.material_line_id.with_context(skip_auto_picking=True).write({
                'taken_qty': new_taken
            })

        # Post notification to job chatter if there were partial/missing materials
        if messages:
            taken_items = []
            for line in lines_to_take:
                taken_items.append(f"• {line.product_id.name}: {line.take_qty} {line.product_uom_id.name if line.product_uom_id else ''}")

            success_msg = _("<b>✅ Успешно превземено:</b><br/>") + "<br/>".join(taken_items)
            messages.insert(0, success_msg)

            job.message_post(
                body=Markup("<br/><br/>".join(messages)),
                message_type='notification',
                subtype_xmlid='mail.mt_note',
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
    )
    product_id = fields.Many2one(
        'product.product',
        string='Производ',
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Мерна единица',
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот',
    )
    planned_qty = fields.Float(
        string='Планирано',
        digits='Product Unit of Measure',
    )
    already_taken_qty = fields.Float(
        string='Веќе земено',
        digits='Product Unit of Measure',
    )
    qty_to_take = fields.Float(
        string='За превземање',
        digits='Product Unit of Measure',
    )
    available_qty = fields.Float(
        string='На залиха',
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
    ], string='Статус')

    @api.constrains('take_qty', 'available_qty', 'qty_to_take')
    def _check_take_qty(self):
        """Validate take quantity"""
        for line in self:
            if line.take_qty < 0:
                raise ValidationError(_('Количината не може да биде негативна.'))
            if line.take_qty > line.available_qty and line.product_id:
                raise ValidationError(_(
                    'Количината за превземање (%s) не може да биде поголема од залихата (%s) за %s'
                ) % (line.take_qty, line.available_qty, line.product_id.name))

    def action_take_line(self):
        """Take just this single line"""
        self.ensure_one()

        if not self.material_line_id or not self.product_id:
            raise ValidationError(_('Невалидна линија.'))

        if self.take_qty <= 0:
            raise ValidationError(_('Внесете количина за превземање.'))

        if self.take_qty > self.available_qty:
            raise ValidationError(_(
                'Количината за превземање (%s) не може да биде поголема од залихата (%s)'
            ) % (self.take_qty, self.available_qty))

        job = self.wizard_id.job_id

        # Use StockPickingService to create picking for single line
        picking_service = self.env['esfsm.stock.picking.service']
        picking = picking_service.create_reverse_picking(job, self)

        # Update material line taken_qty
        new_taken = self.material_line_id.taken_qty + self.take_qty
        self.material_line_id.with_context(skip_auto_picking=True).write({
            'taken_qty': new_taken
        })

        # Post to chatter
        job.message_post(
            body=Markup(_("<b>✅ Превземено:</b><br/>• %s: %s %s") % (
                self.product_id.name,
                self.take_qty,
                self.product_uom_id.name if self.product_uom_id else ''
            )),
            message_type='notification',
            subtype_xmlid='mail.mt_note',
        )

        # Update wizard line status
        self.write({
            'already_taken_qty': new_taken,
            'qty_to_take': self.planned_qty - new_taken,
            'take_qty': 0,
            'status': 'ok' if new_taken >= self.planned_qty else 'partial',
        })

        # Return action to stay in wizard
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'esfsm.take.material.wizard',
            'res_id': self.wizard_id.id,
            'view_mode': 'form',
            'target': 'new',
        }

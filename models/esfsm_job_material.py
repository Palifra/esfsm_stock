# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmJobMaterial(models.Model):
    _name = 'esfsm.job.material'
    _description = 'Материјал за работа'
    _order = 'sequence, id'

    job_id = fields.Many2one(
        'esfsm.job',
        string='Работа',
        required=True,
        ondelete='cascade',
        help='Работа за која е овој материјал'
    )
    sequence = fields.Integer(
        string='Редослед',
        default=10,
        help='Редослед на прикажување'
    )
    product_id = fields.Many2one(
        'product.product',
        string='Производ',
        required=True,
        domain=[('type', 'in', ['consu', 'product'])],
        help='Материјал/производ'
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Мерна единица',
        required=True,
        help='Мерна единица за овој материјал'
    )

    # Lifecycle quantity fields
    planned_qty = fields.Float(
        string='Планирана количина',
        default=0.0,
        digits='Product Unit of Measure',
        help='Проценета количина потребна за работата'
    )
    taken_qty = fields.Float(
        string='Земена количина',
        default=0.0,
        digits='Product Unit of Measure',
        help='Количина земена од магацин/возило (преку picking)'
    )
    used_qty = fields.Float(
        string='Искористена количина',
        default=0.0,
        digits='Product Unit of Measure',
        help='Количина искористена на работата (consumption)'
    )
    returned_qty = fields.Float(
        string='Вратена количина',
        default=0.0,
        digits='Product Unit of Measure',
        help='Неискористена количина вратена назад'
    )

    # Computed field for returns
    available_to_return_qty = fields.Float(
        string='Достапно за враќање',
        compute='_compute_available_to_return_qty',
        digits='Product Unit of Measure',
        help='Количина што може да се врати: taken - used - returned'
    )

    # Price tracking
    price_unit = fields.Float(
        string='Единечна цена',
        digits='Product Price',
        help='Единечна цена на материјалот'
    )
    price_subtotal = fields.Monetary(
        string='Вкупно',
        compute='_compute_price_subtotal',
        store=True,
        help='Вкупна цена (искористена количина * единечна цена)'
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Валута',
        related='job_id.company_id.currency_id',
        readonly=True
    )

    company_id = fields.Many2one(
        'res.company',
        string='Компанија',
        related='job_id.company_id',
        store=True,
        readonly=True
    )

    # Lot tracking for cable products
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот/Сериски број',
        domain="[('product_id', '=', product_id)]",
        help='Лот или сериски број за производи со следење'
    )
    product_tracking = fields.Selection(
        related='product_id.tracking',
        string='Тип на следење',
        readonly=True
    )

    @api.depends('taken_qty', 'used_qty', 'returned_qty')
    def _compute_available_to_return_qty(self):
        """Calculate quantity available to return"""
        for line in self:
            line.available_to_return_qty = line.taken_qty - line.used_qty - line.returned_qty

    @api.depends('used_qty', 'price_unit')
    def _compute_price_subtotal(self):
        """Calculate subtotal based on used quantity"""
        for line in self:
            line.price_subtotal = line.used_qty * line.price_unit

    @api.onchange('product_id')
    def _onchange_product_id(self):
        """Auto-fill UoM and price from product"""
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id
            self.price_unit = self.product_id.standard_price

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-populate product_uom_id and price_unit from product if not provided"""
        for vals in vals_list:
            if vals.get('product_id') and not vals.get('product_uom_id'):
                product = self.env['product.product'].browse(vals['product_id'])
                vals['product_uom_id'] = product.uom_id.id
                if not vals.get('price_unit'):
                    vals['price_unit'] = product.standard_price
        return super().create(vals_list)

    def action_take_material(self):
        """Button action to take planned material (create Реверс)"""
        self.ensure_one()
        if self.planned_qty <= 0:
            raise ValidationError(_('Нема планирана количина за превземање.'))

        # Calculate how much to take (planned minus already taken)
        qty_to_take = self.planned_qty - self.taken_qty
        if qty_to_take <= 0:
            raise ValidationError(_('Целата планирана количина е веќе превземена.'))

        # Update taken_qty - this triggers _create_material_picking via write()
        self.taken_qty = self.planned_qty
        return True

    def action_take_all_materials(self):
        """Button action on job to take all planned materials"""
        for line in self:
            if line.planned_qty > line.taken_qty:
                qty_to_take = line.planned_qty - line.taken_qty
                line.taken_qty = line.planned_qty
        return True

    @api.constrains('used_qty', 'taken_qty')
    def _check_used_quantity(self):
        """Validate that used quantity doesn't exceed taken quantity"""
        for line in self:
            if line.used_qty > line.taken_qty:
                raise ValidationError(_(
                    'Искористената количина (%s) не може да биде поголема од земената количина (%s) за %s'
                ) % (line.used_qty, line.taken_qty, line.product_id.name))

    @api.constrains('returned_qty', 'taken_qty', 'used_qty')
    def _check_returned_quantity(self):
        """Validate that returned quantity doesn't exceed available"""
        for line in self:
            # Only check if there's actually a returned quantity
            if line.returned_qty > 0:
                available = line.taken_qty - line.used_qty
                if line.returned_qty > available:
                    raise ValidationError(_(
                        'Вратената количина (%s) не може да биде поголема од достапната (%s) за %s'
                    ) % (line.returned_qty, available, line.product_id.name))

    def write(self, vals):
        """
        Write method with context flag to skip auto-picking.

        When called from wizards with context={'skip_auto_picking': True},
        the picking creation is skipped because the wizard already handles it.

        Direct edits from UI are blocked by readonly fields in views.
        """
        # Skip auto-picking if called from wizard (wizard already created picking)
        if self.env.context.get('skip_auto_picking'):
            return super().write(vals)

        # For any other write (should not happen due to readonly fields),
        # we still prevent direct quantity changes without proper workflow
        quantity_fields = ['taken_qty', 'used_qty', 'returned_qty']
        changed_qty_fields = [f for f in quantity_fields if f in vals]

        if changed_qty_fields:
            # Log warning - this should not happen in normal operation
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning(
                'Direct write to quantity fields %s on material line %s. '
                'This should be done via wizards.',
                changed_qty_fields, self.ids
            )

        return super().write(vals)

    def _create_material_picking(self, quantity):
        """
        Create stock picking for material take (warehouse → technician).
        Uses "Реверс" picking type with technician name.

        Args:
            quantity (float): Quantity to transfer
        """
        self.ensure_one()

        job = self.job_id

        # Get responsible technician name (or first assigned, or 'Unknown')
        technician_name = (
            job.material_responsible_id.name if job.material_responsible_id
            else job.employee_ids[0].name if job.employee_ids
            else 'Непознат'
        )

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

        # Get source and destination from picking type defaults, or use job locations
        source_location = picking_type.default_location_src_id or self.env.ref('stock.stock_location_stock')
        dest_location = job._get_source_location()

        # Create picking with job link and technician name
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'origin': f"{job.name} - Реверс - {technician_name}",
        })

        # Create stock move
        self.env['stock.move'].create({
            'name': f"{job.name} - {self.product_id.name}",
            'product_id': self.product_id.id,
            'product_uom_qty': quantity,
            'product_uom': self.product_uom_id.id,
            'picking_id': picking.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
        })

        # Auto-validate the picking
        picking.action_confirm()
        picking.action_assign()

        # Set quantities done with lot if applicable
        for move in picking.move_ids:
            if self.lot_id and self.product_id.tracking != 'none':
                # For lot-tracked products, set lot on move lines
                for move_line in move.move_line_ids:
                    move_line.lot_id = self.lot_id
                    move_line.quantity = move.product_uom_qty
                # If no move lines exist, create one with lot
                if not move.move_line_ids:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'product_uom_id': move.product_uom.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'lot_id': self.lot_id.id,
                        'quantity': move.product_uom_qty,
                        'picking_id': picking.id,
                    })
            else:
                # Non-lot tracked products
                move.quantity = move.product_uom_qty

        picking.button_validate()

        # Log in job chatter with lot info
        lot_info = f" (Лот: {self.lot_id.name})" if self.lot_id else ""
        job.message_post(
            body=_('Реверс издаден на %s: %s %s (%s)%s - %s') % (
                technician_name,
                quantity,
                self.product_uom_id.name,
                self.product_id.name,
                lot_info,
                picking.name
            )
        )

        return picking

    def _create_consumption_move(self, quantity):
        """
        Create stock consumption move (technician → customer).
        Uses "Испратници" picking type.

        Args:
            quantity (float): Quantity to consume
        """
        self.ensure_one()

        job = self.job_id

        # Get responsible technician name (or first assigned, or 'Unknown')
        technician_name = (
            job.material_responsible_id.name if job.material_responsible_id
            else job.employee_ids[0].name if job.employee_ids
            else 'Непознат'
        )

        # Get source (vehicle/technician) and destination (customer) locations
        source_location = job._get_source_location()  # Vehicle/technician location
        dest_location = self.env.ref('stock.stock_location_customers')  # Customer/consumption

        # Find outgoing picking type (Испратници)
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('company_id', '=', job.company_id.id)
        ], limit=1)

        # Create picking for consumption with job link
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
            'esfsm_job_id': job.id,
            'partner_id': job.partner_id.id,
            'origin': f"{job.name} - Испратница - {technician_name}",
        })

        # Create stock move
        self.env['stock.move'].create({
            'name': f"{job.name} - {self.product_id.name}",
            'product_id': self.product_id.id,
            'product_uom_qty': quantity,
            'product_uom': self.product_uom_id.id,
            'picking_id': picking.id,
            'location_id': source_location.id,
            'location_dest_id': dest_location.id,
        })

        # Auto-validate the picking
        picking.action_confirm()
        picking.action_assign()

        # Set quantities done with lot if applicable
        for move in picking.move_ids:
            if self.lot_id and self.product_id.tracking != 'none':
                # For lot-tracked products, set lot on move lines
                for move_line in move.move_line_ids:
                    move_line.lot_id = self.lot_id
                    move_line.quantity = move.product_uom_qty
                # If no move lines exist, create one with lot
                if not move.move_line_ids:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'product_uom_id': move.product_uom.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'lot_id': self.lot_id.id,
                        'quantity': move.product_uom_qty,
                        'picking_id': picking.id,
                    })
            else:
                # Non-lot tracked products
                move.quantity = move.product_uom_qty

        picking.button_validate()

        # Log in job chatter with lot info
        lot_info = f" (Лот: {self.lot_id.name})" if self.lot_id else ""
        job.message_post(
            body=_('Испратница од %s кон %s: %s %s (%s)%s - %s') % (
                technician_name,
                job.partner_id.name,
                quantity,
                self.product_uom_id.name,
                self.product_id.name,
                lot_info,
                picking.name
            )
        )

        return picking

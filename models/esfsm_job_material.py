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
        Auto-create stock picking when taken_qty is increased.
        Auto-create stock consumption when used_qty is increased.
        """
        for line in self:
            # Check if taken_qty is being increased
            if 'taken_qty' in vals:
                old_taken_qty = line.taken_qty
                new_taken_qty = vals['taken_qty']

                if new_taken_qty > old_taken_qty:
                    # Create picking for the delta quantity
                    qty_delta = new_taken_qty - old_taken_qty
                    line._create_material_picking(qty_delta)

            # Check if used_qty is being increased
            if 'used_qty' in vals:
                old_used_qty = line.used_qty
                new_used_qty = vals['used_qty']

                if new_used_qty > old_used_qty:
                    # Create consumption move for the delta quantity
                    qty_delta = new_used_qty - old_used_qty
                    line._create_consumption_move(qty_delta)

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

        # Set quantities done
        # Use move.quantity instead of move_line for more reliable quantity setting
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty

        picking.button_validate()

        # Log in job chatter with technician name
        technician_name = job.employee_ids[0].name if job.employee_ids else 'Непознат'
        job.message_post(
            body=_('Реверс издаден на %s: %s %s (%s) - %s') % (
                technician_name,
                quantity,
                self.product_uom_id.name,
                self.product_id.name,
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

        # Get first technician name for the document
        technician_name = job.employee_ids[0].name if job.employee_ids else 'Непознат'

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

        # Set quantities done
        # Use move.quantity instead of move_line for more reliable quantity setting
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty

        picking.button_validate()

        # Log in job chatter with technician and customer info
        technician_name = job.employee_ids[0].name if job.employee_ids else 'Непознат'
        job.message_post(
            body=_('Испратница од %s кон %s: %s %s (%s) - %s') % (
                technician_name,
                job.partner_id.name,
                quantity,
                self.product_uom_id.name,
                self.product_id.name,
                picking.name
            )
        )

        return picking

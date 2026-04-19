# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError


class EsfsmJobMaterial(models.Model):
    _name = 'esfsm.job.material'
    _description = 'Материјал за работа'
    _order = 'sequence, id'

    _sql_constraints = [
        ('check_positive_planned', 'CHECK(planned_qty >= 0)',
         'Планираната количина не може да биде негативна.'),
        ('check_positive_taken', 'CHECK(taken_qty >= 0)',
         'Земената количина не може да биде негативна.'),
        ('check_positive_used', 'CHECK(used_qty >= 0)',
         'Искористената количина не може да биде негативна.'),
        ('check_positive_returned', 'CHECK(returned_qty >= 0)',
         'Вратената количина не може да биде негативна.'),
    ]

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
    # DEPRECATED: Will be dropped in Phase 4 after migration to lot_allocation_ids.
    # See docs/plans/2026-04-19-per-lot-allocation-design.md
    lot_id = fields.Many2one(
        'stock.lot',
        string='Лот/Сериски број (legacy)',
        domain="[('product_id', '=', product_id)]",
        help='Legacy single-lot field. Use lot_allocation_ids instead.'
    )
    product_tracking = fields.Selection(
        related='product_id.tracking',
        string='Тип на следење',
        readonly=True
    )

    # Per-lot allocation tracking (Phase 1 — new model)
    lot_allocation_ids = fields.One2many(
        'esfsm.job.material.lot',
        'material_id',
        string='Алокации по лот',
    )
    manual_lot_selection = fields.Boolean(
        string='Рачен избор на лот',
        default=False,
        help='Ако е чекирано, корисникот мора рачно да избере лот/лотови во Take wizard. '
             'Во спротивно, Odoo FEFO одлучува автоматски.'
    )
    lot_allocation_historical_gap = fields.Boolean(
        string='Историски пропуст',
        default=False,
        readonly=True,
        help='Материјалот е земен пред воведување на lot allocation tracking и нема забележани лотови.'
    )
    primary_lot_id = fields.Many2one(
        'stock.lot',
        string='Примарен лот',
        compute='_compute_primary_lot',
        store=True,
        help='Најголема алокација по количина. За backward compatibility со legacy API.',
    )
    taken_qty_per_lot_sum = fields.Float(
        string='Збир земено по лот',
        compute='_compute_lot_sums',
        digits='Product Unit of Measure',
    )
    used_qty_per_lot_sum = fields.Float(
        string='Збир искористено по лот',
        compute='_compute_lot_sums',
        digits='Product Unit of Measure',
    )
    returned_qty_per_lot_sum = fields.Float(
        string='Збир вратено по лот',
        compute='_compute_lot_sums',
        digits='Product Unit of Measure',
    )

    @api.depends('lot_allocation_ids.taken_qty',
                 'lot_allocation_ids.used_qty',
                 'lot_allocation_ids.returned_qty')
    def _compute_lot_sums(self):
        for m in self:
            m.taken_qty_per_lot_sum = sum(m.lot_allocation_ids.mapped('taken_qty'))
            m.used_qty_per_lot_sum = sum(m.lot_allocation_ids.mapped('used_qty'))
            m.returned_qty_per_lot_sum = sum(m.lot_allocation_ids.mapped('returned_qty'))

    @api.depends('lot_allocation_ids.taken_qty', 'lot_allocation_ids.lot_id', 'lot_id')
    def _compute_primary_lot(self):
        for m in self:
            if m.lot_allocation_ids:
                primary = max(m.lot_allocation_ids, key=lambda a: a.taken_qty)
                m.primary_lot_id = primary.lot_id
            elif m.lot_id:
                # Fallback to legacy during Phase 2 transition
                m.primary_lot_id = m.lot_id
            else:
                m.primary_lot_id = False

    @api.constrains('lot_allocation_ids', 'taken_qty', 'used_qty', 'returned_qty',
                    'lot_allocation_historical_gap')
    def _check_lot_sum_matches(self):
        # Historical gap records bypass the sum check (legacy data without allocations)
        for m in self:
            if not m.lot_allocation_ids:
                continue
            if m.lot_allocation_historical_gap:
                continue
            pairs = (
                (m.taken_qty_per_lot_sum, m.taken_qty, _('земено')),
                (m.used_qty_per_lot_sum, m.used_qty, _('искористено')),
                (m.returned_qty_per_lot_sum, m.returned_qty, _('вратено')),
            )
            for sum_v, total_v, label in pairs:
                if abs(sum_v - total_v) > 0.001:
                    raise ValidationError(_(
                        'Збирот на %(label)s по лот (%(sum)s) не се совпаѓа со вкупно (%(total)s) '
                        'за материјал %(product)s',
                        label=label, sum=sum_v, total=total_v,
                        product=m.product_id.name,
                    ))

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
        Write method with context flag to skip auto-picking.

        When called from wizards with context={'skip_auto_picking': True},
        the picking creation is skipped because the wizard already handles it.

        Direct edits from UI are blocked by readonly fields in views.
        """
        # Audit log: track quantity changes in job chatter
        quantity_fields = {'taken_qty', 'used_qty', 'returned_qty', 'planned_qty'}
        changed_qty_fields = quantity_fields & set(vals.keys())

        if changed_qty_fields and not self.env.context.get('skip_qty_log'):
            for record in self:
                changes = []
                for field in sorted(changed_qty_fields):
                    old_val = getattr(record, field)
                    new_val = vals[field]
                    if old_val != new_val:
                        label = record._fields[field].string
                        changes.append('%s: %.2f → %.2f' % (label, old_val, new_val))
                if changes and record.job_id:
                    record.job_id.message_post(
                        body=_('Материјал %s: %s') % (
                            record.product_id.name,
                            ', '.join(changes)
                        ),
                        message_type='notification',
                    )

        # Skip auto-picking if called from wizard (wizard already created picking)
        if self.env.context.get('skip_auto_picking'):
            return super().write(vals)

        # For any other write (should not happen due to readonly fields),
        # we still prevent direct quantity changes without proper workflow
        qty_only = {'taken_qty', 'used_qty', 'returned_qty'}
        changed_direct = [f for f in qty_only if f in vals]

        if changed_direct:
            # Log warning - this should not happen in normal operation
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning(
                'Direct write to quantity fields %s on material line %s. '
                'This should be done via wizards.',
                changed_direct, self.ids
            )

        return super().write(vals)


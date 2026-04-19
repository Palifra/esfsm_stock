# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

import logging
from collections import defaultdict

from odoo import api, models, fields, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


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
        domain=[('type', '=', 'consu')],
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
    lot_id_legacy_archive = fields.Json(
        string='Legacy lot archive',
        copy=False,
        help='Snapshot of lot_id before Phase 3 migration, for reversibility.',
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
                # Deterministic: largest taken_qty, then lowest id for tie-break
                primary = max(
                    m.lot_allocation_ids,
                    key=lambda a: (a.taken_qty, -a.id),
                )
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
        # Dual-write transactions (Phase 2) suspend this via context flag
        if self.env.context.get('skip_allocation_sum_check'):
            return
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

    # ──────────────────────────────────────────────
    # Phase 2: Dual-write allocation sync helpers
    # ──────────────────────────────────────────────

    @api.model
    def _is_per_lot_enabled(self):
        """Return True if per-lot allocation writes are enabled (Phase 2+).
        Meant to be called once per wizard action; callers should propagate the
        boolean to sync methods via `per_lot_enabled` arg."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'esfsm_stock.per_lot_allocations_enabled', 'False') == 'True'

    def _rounding(self):
        """UoM rounding for precision comparisons. Falls back to 0.001."""
        self.ensure_one()
        return self.product_uom_id.rounding or 0.001

    def _get_or_create_allocation(self, lot, initial_qty=0.0):
        """Upsert an allocation for (self, lot). Handles the UNIQUE-violation race
        by retrying inside a savepoint. Returns the allocation record."""
        self.ensure_one()
        existing = self.lot_allocation_ids.filtered(lambda a: a.lot_id == lot)
        if existing:
            return existing[0]
        Allocation = self.env['esfsm.job.material.lot'].with_context(
            skip_allocation_sum_check=True
        )
        try:
            with self.env.cr.savepoint():
                return Allocation.create({
                    'material_id': self.id,
                    'lot_id': lot.id,
                    'taken_qty': initial_qty,
                })
        except Exception as e:
            # Check if UNIQUE(material_id, lot_id) race — another tx created it first.
            msg = str(e).lower()
            if 'unique_material_lot' in msg or 'duplicate key' in msg:
                self.invalidate_recordset(['lot_allocation_ids'])
                existing = self.env['esfsm.job.material.lot'].search([
                    ('material_id', '=', self.id),
                    ('lot_id', '=', lot.id),
                ], limit=1)
                if not existing:
                    raise
                if initial_qty:
                    existing.with_context(skip_allocation_sum_check=True).taken_qty += initial_qty
                return existing
            raise

    def _validate_allocation_sums(self):
        """Run sum-match check without the skip-flag. Call at end of sync to
        catch silent drift. Raises ValidationError on mismatch."""
        self.with_context(skip_allocation_sum_check=False)._check_lot_sum_matches()

    def _fefo_sort_key(self, alloc):
        """FEFO ordering: earliest expiration first (if product_expiry module is
        installed), fallback to lot create_date, then allocation id for determinism.
        Uses getattr so the code works with or without product_expiry."""
        lot = alloc.lot_id
        far_future = fields.Datetime.from_string('9999-12-31 23:59:59')
        exp = getattr(lot, 'expiration_date', False) or far_future
        created = lot.create_date or far_future
        return (exp, created, alloc.id)

    def _sync_allocation_on_take(self, picking, per_lot_enabled=None):
        """Mirror a validated picking's lot distribution into lot_allocation_ids.
        Idempotent: the picking's esfsm_allocation_synced flag prevents double-sync.
        Post-sync: re-validates sum match; raises on drift."""
        self.ensure_one()
        if per_lot_enabled is None:
            per_lot_enabled = self._is_per_lot_enabled()
        if not per_lot_enabled or self.product_tracking == 'none':
            return
        if picking.state != 'done':
            raise ValidationError(_(
                'Picking %(name)s не е валидиран (state=%(state)s). '
                'Lot alokacijite мора да се sync-аат само после validation.',
                name=picking.name, state=picking.state,
            ))
        if picking.esfsm_allocation_synced:
            # Already processed — idempotent no-op on retry.
            return

        move_lines = picking.move_line_ids.filtered(
            lambda ml: ml.product_id == self.product_id and ml.lot_id and ml.quantity > 0
        )
        if not move_lines:
            return
        lot_qtys = defaultdict(float)
        for ml in move_lines:
            lot_qtys[ml.lot_id] += ml.quantity
        for lot, qty in lot_qtys.items():
            if qty <= 0:
                continue
            alloc = self._get_or_create_allocation(lot, initial_qty=0.0)
            alloc.with_context(skip_allocation_sum_check=True).taken_qty = alloc.taken_qty + qty
        # Mark picking as synced for idempotency on retry
        picking.sudo().esfsm_allocation_synced = True
        # Final validation catches silent drift
        self.invalidate_recordset([
            'taken_qty_per_lot_sum', 'used_qty_per_lot_sum', 'returned_qty_per_lot_sum',
        ])
        self._validate_allocation_sums()

    def _sync_allocation_on_take_explicit(self, lot, qty, per_lot_enabled=None):
        """Explicit lot+qty allocation (Add wizard where user picks the lot)."""
        self.ensure_one()
        if per_lot_enabled is None:
            per_lot_enabled = self._is_per_lot_enabled()
        if not per_lot_enabled or self.product_tracking == 'none':
            return
        if not lot or qty <= 0:
            return
        alloc = self._get_or_create_allocation(lot, initial_qty=0.0)
        alloc.with_context(skip_allocation_sum_check=True).taken_qty = alloc.taken_qty + qty
        self.invalidate_recordset([
            'taken_qty_per_lot_sum', 'used_qty_per_lot_sum', 'returned_qty_per_lot_sum',
        ])
        self._validate_allocation_sums()

    def _distribute_across_allocations(self, total_qty, field, available_field, lot=None):
        """FEFO distribute `total_qty` across allocations, writing `field`.
        Uses a Python snapshot to avoid per-iteration recompute writes (C1 fix).
        Returns the undistributed remainder."""
        self.ensure_one()
        rounding = self._rounding()
        candidates = self.lot_allocation_ids
        if lot:
            candidates = candidates.filtered(lambda a: a.lot_id == lot)
        if not candidates:
            return total_qty

        # Snapshot ordered plan BEFORE mutating anything
        ordered = candidates.sorted(key=lambda a: self._fefo_sort_key(a))
        plan = []
        remaining = total_qty
        for alloc in ordered:
            if float_is_zero(remaining, precision_rounding=rounding):
                break
            avail = getattr(alloc, available_field)
            if avail <= 0:
                continue
            take = min(remaining, avail)
            plan.append((alloc, getattr(alloc, field) + take))
            remaining -= take

        # Apply all writes from the plan (no recompute-read in loop)
        for alloc, new_value in plan:
            alloc.with_context(skip_allocation_sum_check=True).write({field: new_value})
        return remaining

    def _sync_allocation_on_consume(self, consume_qty, lot=None, per_lot_enabled=None):
        """Distribute a consume delta across allocations using FEFO."""
        self.ensure_one()
        if per_lot_enabled is None:
            per_lot_enabled = self._is_per_lot_enabled()
        if not per_lot_enabled or self.product_tracking == 'none':
            return
        if not self.lot_allocation_ids:
            return
        remaining = self._distribute_across_allocations(
            consume_qty, 'used_qty', 'available_to_consume_qty', lot=lot,
        )
        if float_compare(remaining, 0.0, precision_rounding=self._rounding()) > 0:
            _logger.warning(
                'Consume sync incomplete for material %s (%s): undistributed=%s',
                self.id, self.product_id.display_name, remaining,
            )
        self.invalidate_recordset([
            'taken_qty_per_lot_sum', 'used_qty_per_lot_sum', 'returned_qty_per_lot_sum',
        ])
        self._validate_allocation_sums()

    def _sync_allocation_on_return(self, return_qty, lot=None, per_lot_enabled=None):
        """Distribute a return delta across allocations using FEFO."""
        self.ensure_one()
        if per_lot_enabled is None:
            per_lot_enabled = self._is_per_lot_enabled()
        if not per_lot_enabled or self.product_tracking == 'none':
            return
        if not self.lot_allocation_ids:
            return
        remaining = self._distribute_across_allocations(
            return_qty, 'returned_qty', 'available_to_return_qty', lot=lot,
        )
        if float_compare(remaining, 0.0, precision_rounding=self._rounding()) > 0:
            _logger.warning(
                'Return sync incomplete for material %s (%s): undistributed=%s',
                self.id, self.product_id.display_name, remaining,
            )
        self.invalidate_recordset([
            'taken_qty_per_lot_sum', 'used_qty_per_lot_sum', 'returned_qty_per_lot_sum',
        ])
        self._validate_allocation_sums()

    # ──────────────────────────────────────────────
    # Drift detection (cron)
    # ──────────────────────────────────────────────

    @api.model
    def _cron_detect_allocation_drift(self):
        """Daily scan: log materials where sum(allocations) ≠ material totals.
        Does NOT auto-fix — only surfaces drift for manual investigation.
        Skips historical_gap rows."""
        materials = self.sudo().search([
            ('lot_allocation_ids', '!=', False),
            ('lot_allocation_historical_gap', '=', False),
        ])
        drift_count = 0
        for m in materials:
            rounding = m._rounding()
            checks = (
                ('taken', m.taken_qty_per_lot_sum, m.taken_qty),
                ('used', m.used_qty_per_lot_sum, m.used_qty),
                ('returned', m.returned_qty_per_lot_sum, m.returned_qty),
            )
            for label, sum_v, total_v in checks:
                if float_compare(sum_v, total_v, precision_rounding=rounding) != 0:
                    drift_count += 1
                    _logger.warning(
                        'Allocation drift detected: material=%s job=%s product=%s '
                        '%s sum=%s total=%s delta=%s',
                        m.id, m.job_id.name, m.product_id.default_code or m.product_id.name,
                        label, sum_v, total_v, sum_v - total_v,
                    )
                    break
        if drift_count:
            _logger.error(
                'ESFSM allocation drift scan: %s materials with mismatched sums',
                drift_count,
            )
        else:
            _logger.info('ESFSM allocation drift scan: clean (%s materials)', len(materials))
        return drift_count

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


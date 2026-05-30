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
        # Tie-break note: for unsaved records (NewId), id doesn't support
        # arithmetic. Coerce to int or 0 to keep the key comparable under
        # onchange snapshot evaluation.
        def _sort_key(alloc):
            aid = alloc.id if isinstance(alloc.id, int) else 0
            return (alloc.taken_qty, -aid)

        for m in self:
            if m.lot_allocation_ids:
                primary = max(m.lot_allocation_ids, key=_sort_key)
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

        Per-material per-picking idempotency: each allocation tracks which
        pickings contributed to it via source_picking_ids. Re-sync for the
        same (material, picking) pair is a no-op. This replaces the old
        picking-level flag which was buggy when one picking served multiple
        materials — subsequent materials saw `esfsm_allocation_synced=True`
        and skipped their own sync, leaving them with empty allocations."""
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

        # The product-match fallback is intended ONLY for whole-legacy pickings (no move carries esfsm_material_line_id); every move in a newly created picking is always attributed, so mixed-attribution double-count cannot occur.
        move_lines = picking.move_line_ids.filtered(
            lambda ml: ml.quantity > 0 and ml.lot_id and (
                ml.move_id.esfsm_material_line_id == self
                or (not ml.move_id.esfsm_material_line_id
                    and ml.product_id == self.product_id)
            )
        )
        if not move_lines:
            return
        lot_qtys = defaultdict(float)
        for ml in move_lines:
            lot_qtys[ml.lot_id] += ml.quantity
        touched_any = False
        for lot, qty in lot_qtys.items():
            if qty <= 0:
                continue
            alloc = self._get_or_create_allocation(lot, initial_qty=0.0)
            # Per-(material, picking) idempotency guard
            if picking in alloc.source_picking_ids:
                continue
            alloc.with_context(skip_allocation_sum_check=True).taken_qty = alloc.taken_qty + qty
            alloc.with_context(skip_allocation_sum_check=True).source_picking_ids = [(4, picking.id)]
            touched_any = True
        if not touched_any:
            return
        # Retain the picking-level marker for backward audit compatibility
        picking.sudo().esfsm_allocation_synced = True
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
    # Canonical per-material movement methods (Task 6)
    # ──────────────────────────────────────────────
    #
    # apply_take / apply_consume / apply_return are the SINGLE path that both the
    # wizards and (next task) the REST API call so every material movement flows
    # through one stock-move-backed, allocation-synced, validated route. They
    # reuse the existing picking service (create_*_picking_from_lines) and the
    # existing _sync_allocation_on_* / _distribute_across_allocations helpers —
    # no duplication of the distribution/sync logic.
    #
    # The ALLOCATION side of consume/return (capacity guard → pre-set scalar →
    # FEFO distribute → re-derive scalar) is identical between apply_consume /
    # apply_return and the consume/return wizards' per-line allocation branch, so
    # it lives in the two shared helpers below. Only the picking creation differs
    # (apply_* makes a per-material picking, the wizard makes one batch picking),
    # which stays in each caller.

    def _apply_consume_to_allocations(self, qty, lot=False):
        """Distribute a consume of ``qty`` across THIS material's lot allocations.

        Shared by ``apply_consume`` and the consume wizard's per-line allocation
        branch. Caller is responsible for creating the stock-move-backed picking
        (per-material vs batch) — this helper only touches the allocations and
        the ``used_qty`` scalar.

        Sequence (identical for both callers):
          1. Capacity guard FIRST — raise a localized ValidationError on
             over-consume BEFORE any allocation is mutated (so no drift).
          2. Pre-set ``used_qty`` to the post-distribution target
             (``sum(alloc.used_qty) + qty``) so the sum-check inside
             ``_sync_allocation_on_consume`` ties out at validation time.
          3. FEFO-distribute the consume across allocations.
          4. Re-derive ``used_qty`` from the allocation sum (the wizard's trailing
             rebuild loop derives the same value, so there is no double effect).

        Does NOT call ``_validate_allocation_sums`` — callers run that as their
        final integrity check (apply_consume inline, the wizard in its trailing
        loop), matching the prior behavior exactly.
        """
        self.ensure_one()
        per_lot = self._is_per_lot_enabled()
        candidates = self.lot_allocation_ids
        if lot:
            candidates = candidates.filtered(lambda a: a.lot_id == lot)
        rounding = self._rounding()
        available = sum(candidates.mapped('available_to_consume_qty'))
        if float_compare(qty, available, precision_rounding=rounding) > 0:
            raise ValidationError(_(
                'Не може да се распредели потрошувачка од %(qty).2f на '
                '%(product)s по достапните лот-алокации (достапно само '
                '%(avail).2f).',
                qty=qty, product=self.product_id.name, avail=available,
            ))
        material_ctx = self.with_context(
            skip_auto_picking=True,
            skip_allocation_sum_check=True,
        )
        current_alloc_used = sum(self.lot_allocation_ids.mapped('used_qty'))
        material_ctx.used_qty = current_alloc_used + qty
        self.invalidate_recordset(['used_qty'])
        self._sync_allocation_on_consume(
            qty, lot=lot or False, per_lot_enabled=per_lot)
        material_ctx.used_qty = sum(self.lot_allocation_ids.mapped('used_qty'))

    def _apply_return_to_allocations(self, qty, lot=False):
        """Distribute a return of ``qty`` across THIS material's lot allocations.

        Mirror of ``_apply_consume_to_allocations`` for the return leg. Shared by
        ``apply_return`` and the return wizard's per-line allocation branch.
        Caller creates the picking; this helper only touches the allocations and
        the ``returned_qty`` scalar. Does NOT call ``_validate_allocation_sums``
        (callers run it as their final check), matching prior behavior.
        """
        self.ensure_one()
        per_lot = self._is_per_lot_enabled()
        candidates = self.lot_allocation_ids
        if lot:
            candidates = candidates.filtered(lambda a: a.lot_id == lot)
        rounding = self._rounding()
        available = sum(candidates.mapped('available_to_return_qty'))
        if float_compare(qty, available, precision_rounding=rounding) > 0:
            raise ValidationError(_(
                'Не може да се распредели враќање од %(qty).2f на '
                '%(product)s по достапните лот-алокации (достапно само '
                '%(avail).2f).',
                qty=qty, product=self.product_id.name, avail=available,
            ))
        material_ctx = self.with_context(
            skip_auto_picking=True,
            skip_allocation_sum_check=True,
        )
        current_alloc_returned = sum(
            self.lot_allocation_ids.mapped('returned_qty'))
        material_ctx.returned_qty = current_alloc_returned + qty
        self.invalidate_recordset(['returned_qty'])
        self._sync_allocation_on_return(
            qty, lot=lot or False, per_lot_enabled=per_lot)
        material_ctx.returned_qty = sum(
            self.lot_allocation_ids.mapped('returned_qty'))

    def apply_take(self, qty, lot=False):
        """Take ``qty`` of THIS material warehouse → vehicle (Реверс).

        Creates a validated Реверс picking via the picking service, increments
        ``taken_qty``, syncs per-lot allocations on take, and validates sums.

        Args:
            qty: quantity to take.
            lot: stock.lot (required for lot/serial-tracked products).

        Returns:
            stock.picking: the validated picking, or an empty recordset when qty
            is zero (no-op).
        """
        self.ensure_one()
        rounding = self._rounding()
        if float_is_zero(qty, precision_rounding=rounding):
            return self.env['stock.picking']

        job = self.job_id
        picking_service = self.env['esfsm.stock.picking.service']
        material_lines = [{
            'material_line_id': self,
            'product_id': self.product_id,
            'product_uom_id': self.product_uom_id,
            'quantity': qty,
            'lot_id': lot if lot else False,
        }]
        picking = picking_service.create_reverse_picking_from_lines(
            job, material_lines)

        per_lot = self._is_per_lot_enabled()
        material_ctx = self.with_context(
            skip_auto_picking=True,
            skip_allocation_sum_check=True,
        )
        # Mirror the take wizard: back-fill legacy lot_id if missing and bump the
        # scalar before syncing allocations.
        vals = {'taken_qty': material_ctx.taken_qty + qty}
        if (not material_ctx.lot_id
                and material_ctx.product_id.tracking != 'none' and lot):
            vals['lot_id'] = lot.id
        material_ctx.write(vals)
        # Sync per-lot allocations from the validated picking (respects flag and
        # untracked products internally) then hard-validate sums.
        material_ctx._sync_allocation_on_take(picking, per_lot_enabled=per_lot)
        self._validate_allocation_sums()
        return picking

    def apply_consume(self, qty, lot=False):
        """Consume ``qty`` of THIS material vehicle → customer (Испратница).

        Creates a validated Испратница picking, increments ``used_qty``, syncs
        per-lot allocations on consume (FEFO, or lot-scoped when ``lot`` given),
        and validates sums. Honors the capacity guard (over-consume raises a
        localized ValidationError BEFORE any stock move, so no drift).

        Returns:
            stock.picking: the validated picking, or empty recordset when qty is
            zero (no-op).
        """
        self.ensure_one()
        rounding = self._rounding()
        if float_is_zero(qty, precision_rounding=rounding):
            return self.env['stock.picking']

        per_lot = self._is_per_lot_enabled()

        # Capacity guard FIRST — never move stock we cannot account for.
        if per_lot and self.lot_allocation_ids:
            candidates = self.lot_allocation_ids
            if lot:
                candidates = candidates.filtered(lambda a: a.lot_id == lot)
            available = sum(candidates.mapped('available_to_consume_qty'))
        else:
            available = self.taken_qty - self.used_qty - self.returned_qty
        if float_compare(qty, available, precision_rounding=rounding) > 0:
            raise ValidationError(_(
                'Не може да се потроши %(qty).2f на %(product)s — достапно само '
                '%(avail).2f.',
                qty=qty, product=self.product_id.name, avail=available,
            ))

        job = self.job_id
        picking_service = self.env['esfsm.stock.picking.service']
        material_lines = [{
            'material_line_id': self,
            'product_id': self.product_id,
            'product_uom_id': self.product_uom_id,
            'quantity': qty,
            'lot_id': lot if lot else False,
        }]
        picking = picking_service.create_delivery_picking_from_lines(
            job, material_lines)

        if per_lot and self.lot_allocation_ids:
            # Allocation distribution shared with the consume wizard. The guard
            # above already vetted capacity, so the helper's guard is a no-op pass.
            self._apply_consume_to_allocations(qty, lot=lot or False)
        else:
            # Legacy / untracked / flag-off: just the scalar.
            self.with_context(
                skip_auto_picking=True,
                skip_allocation_sum_check=True,
            ).used_qty = self.used_qty + qty

        self._validate_allocation_sums()
        return picking

    def apply_return(self, qty, lot=False):
        """Return ``qty`` of THIS material vehicle → warehouse (Повратница).

        Creates a validated Повратница picking, increments ``returned_qty``,
        syncs per-lot allocations on return (FEFO, or lot-scoped when ``lot``
        given), and validates sums. Honors the capacity guard
        (returned ≤ taken − used) BEFORE moving stock, so no drift.

        Returns:
            stock.picking: the validated picking, or empty recordset when qty is
            zero (no-op).
        """
        self.ensure_one()
        rounding = self._rounding()
        if float_is_zero(qty, precision_rounding=rounding):
            return self.env['stock.picking']

        per_lot = self._is_per_lot_enabled()

        # Capacity guard FIRST.
        if per_lot and self.lot_allocation_ids:
            candidates = self.lot_allocation_ids
            if lot:
                candidates = candidates.filtered(lambda a: a.lot_id == lot)
            available = sum(candidates.mapped('available_to_return_qty'))
        else:
            available = self.taken_qty - self.used_qty - self.returned_qty
        if float_compare(qty, available, precision_rounding=rounding) > 0:
            raise ValidationError(_(
                'Не може да се врати %(qty).2f на %(product)s — достапно само '
                '%(avail).2f.',
                qty=qty, product=self.product_id.name, avail=available,
            ))

        job = self.job_id
        picking_service = self.env['esfsm.stock.picking.service']
        material_lines = [{
            'material_line_id': self,
            'product_id': self.product_id,
            'product_uom_id': self.product_uom_id,
            'quantity': qty,
            'lot_id': lot if lot else False,
        }]
        picking = picking_service.create_return_picking_from_lines(
            job, material_lines)

        if per_lot and self.lot_allocation_ids:
            # Allocation distribution shared with the return wizard. The guard
            # above already vetted capacity, so the helper's guard is a no-op pass.
            self._apply_return_to_allocations(qty, lot=lot or False)
        else:
            self.with_context(
                skip_auto_picking=True,
                skip_allocation_sum_check=True,
            ).returned_qty = self.returned_qty + qty

        self._validate_allocation_sums()
        return picking

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
        """Validate that used quantity doesn't exceed taken quantity (UoM-rounded)."""
        for line in self:
            if not line.product_uom_id:
                continue
            rounding = line.product_uom_id.rounding or 0.001
            if float_compare(line.used_qty, line.taken_qty,
                             precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    'Искористената количина (%(used)s) не може да биде поголема од '
                    'земената количина (%(taken)s) за %(product)s',
                    used=line.used_qty, taken=line.taken_qty,
                    product=line.product_id.name,
                ))

    @api.constrains('returned_qty', 'taken_qty', 'used_qty')
    def _check_returned_quantity(self):
        """Validate that returned quantity doesn't exceed available (UoM-rounded)."""
        for line in self:
            if not line.product_uom_id or line.returned_qty <= 0:
                continue
            rounding = line.product_uom_id.rounding or 0.001
            available = line.taken_qty - line.used_qty
            if float_compare(line.returned_qty, available,
                             precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    'Вратената количина (%(ret)s) не може да биде поголема од '
                    'достапната (%(avail)s) за %(product)s',
                    ret=line.returned_qty, avail=available,
                    product=line.product_id.name,
                ))

    def write(self, vals):
        """
        Write method with context flag to skip auto-picking.

        When called from wizards / apply_* with context
        {'skip_auto_picking': True}, the picking creation is skipped because the
        caller already created (and validated) the stock picking and synced the
        per-lot allocations.

        Direct edits from the UI are blocked by readonly fields in the views
        (taken/used/returned on both list & form, and the embedded per-lot
        allocation taken_qty — Task 9). A DIRECT write (without
        ``skip_auto_picking``) of taken/used/returned is logged as a WARNING but
        NOT hard-rejected: the production audit (Task 9) confirms every
        legitimate caller passes skip_auto_picking=True (take/consume/return/add
        wizards, apply_take/apply_consume/apply_return, and the esfsm_api
        endpoints that route through them; the esfsm_api sync allowlist excludes
        these three fields by design — Task 7/8), but several pre-existing tests
        fabricate state via raw writes, so escalating to a hard error would
        break the suite. planned_qty (a planning estimate) stays directly
        editable.
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

        # Any other write reaching here is a DIRECT (non-wizard / non-apply_*)
        # write of a stock-ledger-backed quantity field. Production code NEVER
        # does this — every legitimate caller (take/consume/return/add wizards,
        # apply_take/apply_consume/apply_return, and the esfsm_api endpoints that
        # route through them) passes skip_auto_picking=True, and the esfsm_api
        # sync allowlist explicitly EXCLUDES these three fields (Task 7/8). The
        # views already render them readonly (defence in the UI).
        #
        # NOTE (Task 9): hard-raising a UserError here was evaluated. The
        # *production* audit is CLEAN, but several PRE-EXISTING tests fabricate
        # state with a raw `write({'used_qty': ...})` (or only
        # `skip_allocation_sum_check`), so escalating to a hard error changed the
        # suite's failure count. Per the conservative mandate we keep this as a
        # WARNING (not a hard error); the UI readonly fields + the esfsm_api sync
        # allowlist remain the enforced guards.
        qty_only = {'taken_qty', 'used_qty', 'returned_qty'}
        changed_direct = [f for f in qty_only if f in vals]

        if changed_direct:
            _logger.warning(
                'Direct write to quantity fields %s on material line %s '
                'WITHOUT skip_auto_picking. Quantity changes must go through the '
                'take/consume/return wizards or the apply_* methods.',
                changed_direct, self.ids,
            )

        return super().write(vals)


# -*- coding: utf-8 -*-
# Part of ESFSM Stock. See LICENSE file for full copyright and licensing details.

"""Manual resolution wizard for Phase 3 ambiguous cases.

An ambiguous combo is (job_id, product_id) where more than one material line
exists for the same product in the same job. Migration can't automatically
attribute picking lots to specific material lines — the user must choose.

Flow:
  1. Wizard default_get collects next unresolved combo.
  2. Shows all material lines × all distinct lots from picking history.
  3. User can either:
     - Resolve: distribute lot qtys across materials (sum must match taken_qty)
     - Mark as Gap: if picking history is insufficient, flag all materials
       in the combo as historical_gap (skipped sum check, legacy lot_id kept)
     - Skip: move to next combo without changes
"""

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class EsfsmLotResolutionWizard(models.TransientModel):
    _name = 'esfsm.lot.resolution.wizard'
    _description = 'Wizard за мануелно resolution на ambiguous lot allocations'

    job_id = fields.Many2one('esfsm.job', string='Работа', readonly=True)
    product_id = fields.Many2one('product.product', string='Производ', readonly=True)
    # material_ids removed from view — legacy BasicModel crashes on
    # invisible Many2many with half-initialized state. We derive materials
    # on-demand from line_ids.mapped('material_id') instead.
    line_ids = fields.One2many(
        'esfsm.lot.resolution.wizard.line',
        'wizard_id',
        string='Алокации',
    )
    remaining_combos = fields.Integer(
        string='Преостанати combos',
        readonly=True,
    )
    total_material_taken = fields.Float(
        string='Вкупно земено (сите материјали)',
        readonly=True,
        digits='Product Unit of Measure',
    )
    total_lot_qty = fields.Float(
        string='Вкупно од лотови (picking history)',
        readonly=True,
        digits='Product Unit of Measure',
    )
    shortage = fields.Float(
        string='Недостаток',
        compute='_compute_shortage',
        digits='Product Unit of Measure',
        help='Ако > 0, picking history не е доволен за да ја покрие земената количина. '
             'Mark as Gap наместо Resolve.',
    )

    @api.depends('total_material_taken', 'total_lot_qty')
    def _compute_shortage(self):
        for w in self:
            w.shortage = max(w.total_material_taken - w.total_lot_qty, 0.0)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        combo = self._find_next_ambiguous()
        if not combo:
            # All combos resolved — initialize scalars; no invisible relational
            # fields in view any more.
            res.update({
                'line_ids': [],
                'job_id': False,
                'product_id': False,
                'total_material_taken': 0.0,
                'total_lot_qty': 0.0,
                'remaining_combos': 0,
            })
            return res

        job_id, product_id, material_ids = combo
        res['job_id'] = job_id
        res['product_id'] = product_id

        materials = self.env['esfsm.job.material'].browse(material_ids)
        migration = self.env['esfsm.lot.allocation.migration']

        # Single query per combo — not per material (fixes 3x amplification bug)
        lot_qtys = migration._get_picking_lot_qtys(materials[:1])
        # Note: _get_picking_lot_qtys takes one material but queries by its job+product,
        # which is shared across all materials in this combo.

        # Build cross-product rows: one line per (material, lot) pair
        lines = []
        for material in materials:
            for lot_id, lot_total in lot_qtys.items():
                lines.append((0, 0, {
                    'material_id': material.id,
                    'lot_id': lot_id,
                    'material_taken_qty': material.taken_qty,
                    'lot_total_qty': lot_total,
                    'qty': 0.0,
                }))
        res['line_ids'] = lines

        res['total_material_taken'] = sum(materials.mapped('taken_qty'))
        res['total_lot_qty'] = sum(lot_qtys.values())

        # Remaining after this one
        stats = migration._classify_materials()
        res['remaining_combos'] = max(len(stats['ambiguous']) - 1, 0)
        return res

    @api.model
    def _find_next_ambiguous(self):
        """Return (job_id, product_id, material_ids) for next unresolved combo, or None.
        A combo is 'resolved' if all materials have allocations OR all are
        flagged as historical_gap."""
        stats = self.env['esfsm.lot.allocation.migration']._classify_materials()
        for combo in stats['ambiguous']:
            materials = self.env['esfsm.job.material'].browse(combo['material_ids'])
            if all(m.lot_allocation_ids or m.lot_allocation_historical_gap
                   for m in materials):
                continue
            return (combo['job_id'], combo['product_id'], combo['material_ids'])
        return None

    def _next_action(self, message=None):
        """After resolving/gapping/skipping, open next combo or finish."""
        next_combo = self._find_next_ambiguous()
        if next_combo:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'esfsm.lot.resolution.wizard',
                'view_mode': 'form',
                'target': 'new',
                'name': _('Следна ambiguous алокација'),
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Готово'),
                'message': message or _('Сите ambiguous combos се resolved.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_resolve(self):
        """Create allocations from user input. Validates per-material sums."""
        self.ensure_one()
        migration = self.env['esfsm.lot.allocation.migration']
        Allocation = self.env['esfsm.job.material.lot']

        by_material = {}
        for line in self.line_ids:
            by_material.setdefault(line.material_id.id, []).append(line)

        # Validate each material's allocations sum to taken_qty (UoM-rounded)
        for mid, lines in by_material.items():
            material = self.env['esfsm.job.material'].browse(mid)
            total = sum(l.qty for l in lines)
            rounding = material._rounding()
            if float_compare(total, material.taken_qty,
                             precision_rounding=rounding) != 0:
                raise ValidationError(_(
                    'Сумата на алокации (%(total).2f) за материјал %(name)s '
                    'не се совпаѓа со taken_qty (%(taken).2f). '
                    'Ако нема доволно lot data, користи "Mark as Gap".',
                    total=total, name=material.product_id.name,
                    taken=material.taken_qty,
                ))

        # Write allocations
        created = 0
        for mid, lines in by_material.items():
            material = self.env['esfsm.job.material'].browse(mid)
            migration._snapshot_legacy(material)
            total_alloc = sum(l.qty for l in lines)
            for line in lines:
                if line.qty <= 0:
                    continue
                ratio = line.qty / total_alloc if total_alloc else 0
                Allocation.with_context(skip_allocation_sum_check=True).create({
                    'material_id': material.id,
                    'lot_id': line.lot_id.id,
                    'taken_qty': line.qty,
                    'used_qty': material.used_qty * ratio,
                    'returned_qty': material.returned_qty * ratio,
                })
                created += 1
            material.invalidate_recordset()
            material._validate_allocation_sums()

        return self._next_action(_('Resolved %d allocations.') % created)

    def action_mark_as_gap(self):
        """Flag all materials in combo as historical_gap. Used when picking
        history is insufficient to explain taken_qty (e.g., legacy data).
        Materials are derived from line_ids (one row per material×lot pair)."""
        self.ensure_one()
        migration = self.env['esfsm.lot.allocation.migration']
        materials = self.line_ids.mapped('material_id')
        for material in materials:
            migration._snapshot_legacy(material)
            material.with_context(skip_allocation_sum_check=True).write({
                'lot_allocation_historical_gap': True,
            })
        return self._next_action(
            _('Marked %d materials as historical gap.') % len(materials)
        )

    def action_skip(self):
        """Skip this combo without changes (it will reappear in future runs)."""
        self.ensure_one()
        return self._next_action(_('Skipped combo.'))


class EsfsmLotResolutionWizardLine(models.TransientModel):
    _name = 'esfsm.lot.resolution.wizard.line'
    _description = 'Линија за ambiguous resolution'

    wizard_id = fields.Many2one(
        'esfsm.lot.resolution.wizard',
        required=True,
        ondelete='cascade',
    )
    material_id = fields.Many2one(
        'esfsm.job.material',
        required=True,
        readonly=True,
    )
    lot_id = fields.Many2one(
        'stock.lot',
        required=True,
        readonly=True,
    )
    material_taken_qty = fields.Float(
        string='Material taken',
        readonly=True,
        digits='Product Unit of Measure',
    )
    lot_total_qty = fields.Float(
        string='Lot total (picking)',
        readonly=True,
        digits='Product Unit of Measure',
    )
    qty = fields.Float(
        string='Допиши',
        digits='Product Unit of Measure',
    )
